"""Connect to environments and keep their latest ServerProfile."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ..domain.enums import AuthMethod, EnvironmentKind, ProviderKind
from ..domain.errors import PavsError
from ..domain.models import Credentials, Environment, ServerProfile, TlsSettings
from ..infra.storage import ProfileRepository
from ..polarion.connection import ConnectionManager, Progress, Session
from .environments import EnvironmentService
from .roles import Action, RoleDenied


@dataclass
class ConnectOutcome:
    ok: bool
    message: str
    environment: Environment | None = None
    profile: ServerProfile | None = None


class ConnectionService:
    def __init__(self, environments: EnvironmentService, profiles: ProfileRepository,
                 manager: ConnectionManager) -> None:
        self.environments = environments
        self.profiles = profiles
        self.manager = manager
        self._sessions: dict[str, Session] = {}

    def connect(self, env: Environment, progress: Progress | None = None) -> ServerProfile:
        """Blocking: run it in a worker thread from the UI."""
        self.environments._need(Action.RUN_TESTS)
        old = self._sessions.pop(env.id, None)
        if old is not None:
            old.close()
        creds = self.environments.credentials(env.id)
        session = self.manager.connect(env, creds, progress=progress)
        self._sessions[env.id] = session
        self.profiles.add(session.profile)
        return session.profile

    def quick_connect(self, url: str, username: str = "", password: str = "", token: str = "",
                      read_config_files: bool = True, remember: bool = False, verify_tls: bool = True,
                      name: str = "", progress: Progress | None = None,
                      kind: EnvironmentKind = EnvironmentKind.DEVELOPMENT) -> ConnectOutcome:
        """The simple flow: URL + credentials -> connected, projects listed. Blocking (use a worker thread)."""
        username, token = username.strip(), token.strip()
        if not token and not (username and password):
            return ConnectOutcome(False, "Enter a username and password, or a Personal Access Token.")
        method = (AuthMethod.TOKEN_AND_PASSWORD if token and password else
                  AuthMethod.TOKEN if token else AuthMethod.PASSWORD)
        providers = [ProviderKind.REST, ProviderKind.SOAP]
        if read_config_files and password:
            providers.append(ProviderKind.SVN)
        try:
            existing = self.environments.find_by_url(url)
            host = Environment(name="x", base_url=url).base_url.split("//", 1)[1].split("/", 1)[0]
            fields = dict(base_url=url, auth_method=method, username=username or None, providers_enabled=providers,
                          tls=TlsSettings(verify=verify_tls), kind=kind)
            env = existing.model_copy(update=fields) if existing else Environment(name=name or host, **fields)
            env = Environment.model_validate(env.model_dump())
        except (ValidationError, ValueError) as exc:
            msg = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
            return ConnectOutcome(False, f"Check the connection details: {msg}")
        try:
            self.environments.save(env, Credentials(token=token or None, password=password or None),
                                   keep_existing_secret=False, persist=remember)
            profile = self.connect(env, progress)
        except RoleDenied:
            raise
        except PavsError as exc:
            return ConnectOutcome(False, str(exc), env)
        return ConnectOutcome(*self._judge(profile), environment=env, profile=profile)

    @staticmethod
    def _judge(profile: ServerProfile) -> tuple[bool, str]:
        if not profile.reachable:
            why = profile.errors[0] if profile.errors else "the server did not answer"
            return False, f"Cannot reach the server: {why}"
        authed = [p for p in profile.providers if p.state.value == "authenticated" and p.kind.value in ("rest", "soap")]
        if authed:
            who = profile.current_user.id if profile.current_user else "the given account"
            return True, f"Connected to Polarion {profile.version.release or '(unknown version)'} as {who}."
        bad = [p for p in profile.providers if p.state.value == "auth_failed"]
        if bad:
            return False, ("Sign-in failed: " + "; ".join(f"{p.kind.value.upper()}: {p.detail}" for p in bad)
                           + ". Check the username/password or token.")
        off = [f"{p.kind.value.upper()}: {p.detail}" for p in profile.providers]
        return False, "The server is reachable but no sign-in was possible. " + "; ".join(off)

    def session(self, env_id: str) -> Session | None:
        return self._sessions.get(env_id)

    def sync_environment(self, env_id: str) -> Session | None:
        """Point the live session and its Write Guard at the CURRENT saved environment (approved test projects,
        test accounts and environment type may have changed since the connection was opened)."""
        s = self._sessions.get(env_id)
        if s is not None:
            env = self.environments.get(env_id)
            s.environment = env
            s.guard.environment = env
        return s

    def latest_profile(self, env_id: str) -> ServerProfile | None:
        s = self._sessions.get(env_id)
        return s.profile if s else self.profiles.latest(env_id)

    def disconnect_all(self) -> None:
        for s in self._sessions.values():
            s.close()
        self._sessions.clear()
