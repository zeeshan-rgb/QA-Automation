"""Saved Polarion environments + their credentials."""
from __future__ import annotations

from ..domain.errors import ConfigurationError
from ..domain.models import Credentials, Environment
from ..infra.storage import AuditLog, EnvironmentRepository
from ..infra.vault import CredentialVault
from .roles import Action, RoleService


class EnvironmentService:
    def __init__(self, repo: EnvironmentRepository, vault: CredentialVault, audit: AuditLog) -> None:
        self.repo = repo
        self.vault = vault
        self.audit = audit
        self._session_creds: dict[str, Credentials] = {}   # used when the vault is not persistent
        self.roles: RoleService | None = None

    def _need(self, action: Action) -> None:
        if self.roles is not None:
            self.roles.require(action)

    def list(self) -> list[Environment]:
        return self.repo.list()

    def get(self, env_id: str) -> Environment:
        env = self.repo.get(env_id)
        if env is None:
            raise ConfigurationError(f"Unknown environment {env_id}")
        return env

    def find_by_url(self, base_url: str) -> Environment | None:
        url = Environment(name="x", base_url=base_url).base_url.lower()
        return next((e for e in self.repo.list() if e.base_url.lower() == url), None)

    def save(self, env: Environment, creds: Credentials | None = None,
             keep_existing_secret: bool = True, persist: bool = True) -> Environment:
        existing = self.repo.get(env.id)
        self._need(Action.RUN_TESTS)
        if env.kind.value == "production" or (existing is not None and existing.kind.value == "production"):
            self._need(Action.MANAGE_ENVIRONMENTS)
        if existing is not None and (existing.test_projects != env.test_projects or existing.test_accounts != env.test_accounts):
            self._need(Action.APPROVE_WRITES)
        elif existing is None and (env.test_projects or env.test_accounts):
            self._need(Action.APPROVE_WRITES)
        clash = [e for e in self.repo.list() if e.name.lower() == env.name.lower() and e.id != env.id]
        if clash:
            raise ConfigurationError(f"An environment called '{env.name}' already exists.")
        is_new = self.repo.get(env.id) is None
        self.repo.save(env)
        if creds is not None and (creds.token or creds.password):
            merged = creds
            if keep_existing_secret:
                old = self.credentials(env.id) or Credentials()
                merged = Credentials(token=creds.token or old.token, password=creds.password or old.password)
            if persist and self.vault.persistent:
                self.vault.save(env.credential_key, merged)
                self._session_creds.pop(env.id, None)
            else:
                self._session_creds[env.id] = merged
            self.audit.record("credentials.update", "ok", environment_id=env.id, target=env.name,
                              detail={"token": bool(merged.token), "password": bool(merged.password)})
        self.audit.record("environment.create" if is_new else "environment.update", "ok",
                          environment_id=env.id, target=env.name,
                          detail={"url": env.base_url, "kind": env.kind.value})
        return env

    def delete(self, env_id: str) -> None:
        self._need(Action.MANAGE_ENVIRONMENTS)
        env = self.get(env_id)
        self.vault.delete(env.credential_key)
        self._session_creds.pop(env_id, None)
        self.repo.delete(env_id)
        self.audit.record("environment.delete", "ok", environment_id=env_id, target=env.name)

    # ------------------------------------------------------------------ test projects and test accounts (Phase 4)
    def allow_writes_in(self, env_id: str, project_id: str, approved_by: str) -> Environment:
        """The user's explicit decision that this project may receive test data. Audited."""
        env = self.get(env_id)
        if project_id not in env.test_projects:
            env = env.model_copy(update={"test_projects": [*env.test_projects, project_id]})
            self.repo.save(env)
        self.audit.record("writes.allow_project", "ok", environment_id=env_id, target=project_id,
                          detail={"approved_by": approved_by, "environment_kind": env.kind.value})
        return env

    def revoke_writes_in(self, env_id: str, project_id: str) -> Environment:
        env = self.get(env_id)
        env = env.model_copy(update={"test_projects": [p for p in env.test_projects if p != project_id]})
        self.repo.save(env)
        self.audit.record("writes.revoke_project", "ok", environment_id=env_id, target=project_id)
        return env

    def _account_key(self, env: Environment, user: str) -> str:
        return f"{env.credential_key}:acct:{user}"

    def set_test_account(self, env_id: str, role: str, user: str, token: str, persist: bool = False) -> Environment:
        env = self.get(env_id)
        accounts = {**env.test_accounts, role: user}
        env = env.model_copy(update={"test_accounts": accounts})
        self.repo.save(env)
        creds = Credentials(token=token)
        key = self._account_key(env, user)
        if persist and self.vault.persistent:
            self.vault.save(key, creds)
        else:
            self._session_creds[key] = creds
        self.audit.record("testaccount.set", "ok", environment_id=env_id, target=role, detail={"user": user, "stored": persist})
        return env

    def remove_test_account(self, env_id: str, role: str) -> Environment:
        env = self.get(env_id)
        user = env.test_accounts.get(role)
        if user:
            self.vault.delete(self._account_key(env, user))
            self._session_creds.pop(self._account_key(env, user), None)
        env = env.model_copy(update={"test_accounts": {r: u for r, u in env.test_accounts.items() if r != role}})
        self.repo.save(env)
        return env

    def test_account_token(self, env_id: str, user: str) -> str | None:
        env = self.get(env_id)
        key = self._account_key(env, user)
        c = self._session_creds.get(key) or self.vault.load(key)
        return c.token if c else None

    def credentials(self, env_id: str) -> Credentials | None:
        if env_id in self._session_creds:
            return self._session_creds[env_id]
        env = self.repo.get(env_id)
        if env is None:
            return None
        return self.vault.load(env.credential_key)

    def has_credentials(self, env_id: str) -> dict[str, bool]:
        c = self.credentials(env_id) or Credentials()
        return {"token": bool(c.token), "password": bool(c.password)}
