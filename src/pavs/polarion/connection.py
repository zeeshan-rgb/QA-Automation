"""Connection manager: environment + credentials -> ServerProfile.

Steps (each reports progress; a failing step never hides the others):
 1 reachability   2 version (no auth)   3 provider probes   4 authentication
 5 current user + /metadata   6 capability resolution   7 project discovery   8 verification probes
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import httpx

from ..domain.enums import (AuthMethod, CapabilityStatus, ProviderKind, ProviderState,
                            Verification)
from ..domain.errors import AuthenticationFailed, PavsError, PolarionApiError
from ..domain.models import (CapabilityRecord, Credentials, Environment, ProviderStatus,
                             ServerProfile, UserInfo)
from ..infra.logging_setup import get_logger
from ..infra.redaction import SecretRegistry
from . import discovery
from .adapters import AdapterManager, VersionAdapter
from .auth import AuthPlan, plan_auth
from .capabilities import CapabilityRegistry, ResolutionContext
from .rest.client import RestClient, is_jsonapi_response
from .safety import WriteGuard
from .soap.client import SoapClient
from .svn.client import SvnConfigReader
from .transport import ExchangeRecorder, HttpTransport
from .version.detector import VersionDetector
from .version.release import Release

log = get_logger("connection")

SOAP_SERVICES = ("Session", "Security", "Project", "Tracker", "TestManagement")
Progress = Callable[[str, int], None]   # (message, percent)


@dataclass
class Session:
    """Live, authenticated handles for one environment (used by later engines)."""
    environment: Environment
    profile: ServerProfile
    transport: HttpTransport
    guard: WriteGuard
    adapter: VersionAdapter
    rest: RestClient | None = None
    soap: SoapClient | None = None
    svn: SvnConfigReader | None = None
    extras: dict = field(default_factory=dict)

    def close(self) -> None:
        if self.soap is not None and self.soap.session_id:
            try:
                self.soap.logout()
            except PavsError:
                pass
        self.transport.close()


class ConnectionManager:
    def __init__(self, registry: CapabilityRegistry | None = None,
                 adapters: AdapterManager | None = None, audit=None,
                 http_transport: httpx.BaseTransport | None = None) -> None:
        self.registry = registry or CapabilityRegistry.load()
        self.adapters = adapters or AdapterManager()
        self.audit = audit
        self._http_transport = http_transport     # injected by tests (mock server)

    # ------------------------------------------------------------------ public
    def connect(self, env: Environment, creds: Credentials | None,
                progress: Progress | None = None, verify: bool = True) -> Session:
        report = progress or (lambda msg, pct: None)
        if creds is not None:
            SecretRegistry.register(creds.token, creds.password)
        recorder = ExchangeRecorder()
        guard = WriteGuard(env, self.audit)
        transport = HttpTransport(env.tls, env.timeout_seconds, guard, recorder,
                                  transport=self._http_transport)
        profile = ServerProfile(environment_id=env.id)
        session = Session(env, profile, transport, guard, adapter=VersionAdapter())
        try:
            self._run(session, creds, report, verify)
        except PavsError as exc:            # anything unexpected is still reported, not raised
            profile.errors.append(str(exc))
        finally:
            profile.exchanges = recorder.items()
        self._audit(env, profile)
        report("Done", 100)
        return session

    # ------------------------------------------------------------------ steps
    def _run(self, s: Session, creds: Credentials | None, report: Progress, verify: bool) -> None:
        env, profile = s.environment, s.profile

        report("Checking that the server is reachable", 5)
        try:
            resp = s.transport.request("GET", env.base_url + "/", provider="detect")
            profile.reachable = resp.status_code < 500
            if resp.status_code >= 400:
                profile.warnings.append(f"Base URL answered HTTP {resp.status_code}.")
        except PavsError as exc:
            profile.reachable = False
            profile.errors.append(f"Server not reachable: {exc}")
            for kind in env.providers_enabled:
                profile.providers.append(ProviderStatus(kind=kind, state=ProviderState.UNREACHABLE,
                                                        detail=str(exc)))
            return

        report("Detecting the Polarion version", 15)
        detector = VersionDetector(s.transport, env.base_url, self.registry.catalog)
        profile.version = detector.detect()

        try:
            plan = plan_auth(env, creds)
        except PavsError as exc:
            profile.errors.append(str(exc))
            plan = plan_auth(env.model_copy(update={"auth_method": AuthMethod.NONE}), None)

        report("Probing REST, SOAP and SVN", 30)
        states: dict[ProviderKind, ProviderState] = {}
        soap_ops: dict[str, set[str] | None] = {}
        if ProviderKind.REST in env.providers_enabled:
            states[ProviderKind.REST] = self._probe_rest(s, plan, profile)
        if ProviderKind.SOAP in env.providers_enabled:
            states[ProviderKind.SOAP] = self._probe_soap(s, plan, profile, soap_ops)
        if ProviderKind.SVN in env.providers_enabled:
            states[ProviderKind.SVN] = self._probe_svn(s, plan, profile)
        for kind in (ProviderKind.BROWSER, ProviderKind.DATABASE):
            if kind in env.providers_enabled:
                states[kind] = ProviderState.NOT_CONFIGURED
                profile.providers.append(ProviderStatus(kind=kind, state=ProviderState.NOT_CONFIGURED,
                                                        detail=("browser checks are switched on per test run, not here"
                                                                if kind == ProviderKind.BROWSER else "a database provider is not built")))

        report("Reading the signed-in user and server metadata", 50)
        if states.get(ProviderKind.REST) == ProviderState.AUTHENTICATED and s.rest is not None:
            try:
                profile.current_user = discovery.current_user_via_rest(s.rest)
            except PavsError as exc:
                profile.warnings.append(f"Current user via REST failed: {exc}")
            meta_op = self.registry.catalog.get("GET /metadata")
            rel = Release.try_parse(profile.version.release)
            if detector.live_rest is not None:
                has_meta = ("GET", "/metadata") in detector.live_rest
            else:
                has_meta = bool(meta_op and (rel is None or meta_op.exists_in(rel)))
            if has_meta:
                try:
                    signals = detector.metadata_signals(s.rest.metadata)
                    if signals:
                        profile.version = VersionDetector.combine(
                            profile.version.signals + [sig for sig, _ in signals],
                            signals[0][1] or profile.version.build)
                except PavsError as exc:
                    profile.warnings.append(f"REST /metadata failed: {exc}")
        if profile.current_user is None and s.soap is not None and s.soap.logged_in_user:
            profile.current_user = UserInfo(id=s.soap.logged_in_user)

        release = Release.try_parse(profile.version.release)
        s.adapter, notes = self.adapters.select(release)
        profile.adapter = s.adapter.name
        profile.warnings.extend(profile.version.warnings + notes + s.adapter.warnings())

        s.extras["live_rest"] = detector.live_rest
        s.extras["soap_ops"] = soap_ops

        report("Resolving capabilities", 65)
        ctx = ResolutionContext(release=release, providers=states, live_rest=detector.live_rest,
                                soap_ops=soap_ops, svn_enabled=ProviderKind.SVN in env.providers_enabled)
        records = [s.adapter.adjust(r) for r in self.registry.resolve(ctx)]

        report("Discovering projects", 75)
        self._discover_projects(s, states, profile)

        if verify:
            report("Verifying capabilities on this server", 85)
            records = self._verify(s, records)
        profile.capabilities = records

    # ------------------------------------------------------------------ providers
    def _probe_rest(self, s: Session, plan: AuthPlan, profile: ServerProfile) -> ProviderState:
        rest = RestClient(s.transport, s.environment.base_url, plan.rest_token)
        try:
            resp = rest.send("GET", "/projects", params={"page[size]": 1, "fields[projects]": "id"},
                             authenticated=False)
        except PavsError as exc:
            profile.providers.append(ProviderStatus(kind=ProviderKind.REST,
                                                    state=ProviderState.UNREACHABLE, detail=str(exc)))
            return ProviderState.UNREACHABLE
        if not (resp.status_code in (200, 401, 403) and is_jsonapi_response(resp)):
            detail = (f"HTTP {resp.status_code} without a JSON:API body: REST is switched off "
                      "(com.siemens.polarion.rest.enabled=false) or blocked by a proxy.")
            profile.providers.append(ProviderStatus(kind=ProviderKind.REST,
                                                    state=ProviderState.DISABLED, detail=detail))
            return ProviderState.DISABLED
        if not plan.can_authenticate(ProviderKind.REST):
            profile.providers.append(ProviderStatus(kind=ProviderKind.REST, state=ProviderState.AVAILABLE,
                                                    detail=plan.explain(ProviderKind.REST)))
            return ProviderState.AVAILABLE
        resp = rest.send("GET", "/user", params={"fields[users]": "id"})
        if resp.status_code == 200:
            s.rest = rest
            profile.providers.append(ProviderStatus(kind=ProviderKind.REST,
                                                    state=ProviderState.AUTHENTICATED, detail="JWT bearer token accepted" if plan.rest_token_is_jwt else "token accepted"))
            return ProviderState.AUTHENTICATED
        if resp.status_code == 403:
            s.rest = rest
            profile.providers.append(ProviderStatus(
                kind=ProviderKind.REST, state=ProviderState.AUTHENTICATED,
                detail="token accepted, but this user lacks REST endpoint permissions (403)"))
            return ProviderState.AUTHENTICATED
        profile.providers.append(ProviderStatus(kind=ProviderKind.REST, state=ProviderState.AUTH_FAILED,
                                                detail=f"token rejected (HTTP {resp.status_code})"))
        return ProviderState.AUTH_FAILED

    def _probe_soap(self, s: Session, plan: AuthPlan, profile: ServerProfile,
                    soap_ops: dict[str, set[str] | None]) -> ProviderState:
        soap = SoapClient(s.transport, s.environment.base_url)
        try:
            for service in SOAP_SERVICES:
                soap_ops[service] = soap.wsdl_operations(service)
        except PavsError as exc:
            profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP,
                                                    state=ProviderState.UNREACHABLE, detail=str(exc)))
            return ProviderState.UNREACHABLE
        if not soap_ops.get("Session"):
            profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP, state=ProviderState.DISABLED,
                                                    detail="SessionWebService WSDL not served"))
            return ProviderState.DISABLED
        if not plan.can_authenticate(ProviderKind.SOAP):
            profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP, state=ProviderState.AVAILABLE,
                                                    detail=plan.explain(ProviderKind.SOAP)))
            return ProviderState.AVAILABLE
        try:
            if plan.soap_token:
                soap.login_token(plan.soap_token)
            else:
                soap.login_password(plan.soap_user or "", plan.soap_password or "")
                soap.logged_in_user = plan.soap_user
        except AuthenticationFailed as exc:
            profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP, state=ProviderState.AUTH_FAILED,
                                                    detail=exc.detail or "authentication failed"))
            return ProviderState.AUTH_FAILED
        except PavsError as exc:
            profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP, state=ProviderState.AUTH_FAILED,
                                                    detail=str(exc)))
            return ProviderState.AUTH_FAILED
        s.soap = soap
        how = "token" if plan.soap_token else "password"
        profile.providers.append(ProviderStatus(kind=ProviderKind.SOAP, state=ProviderState.AUTHENTICATED,
                                                detail=f"signed in with {how}"))
        return ProviderState.AUTHENTICATED

    def _probe_svn(self, s: Session, plan: AuthPlan, profile: ServerProfile) -> ProviderState:
        svn = SvnConfigReader(s.transport, s.environment.server_root, plan.svn_user, plan.svn_password)
        try:
            resp = svn.probe()
        except PavsError as exc:
            profile.providers.append(ProviderStatus(kind=ProviderKind.SVN,
                                                    state=ProviderState.UNREACHABLE, detail=str(exc)))
            return ProviderState.UNREACHABLE
        if resp.status_code == 404:
            profile.providers.append(ProviderStatus(kind=ProviderKind.SVN, state=ProviderState.DISABLED,
                                                    detail="/repo not served"))
            return ProviderState.DISABLED
        if resp.status_code == 401:
            state = ProviderState.AUTH_FAILED if plan.can_authenticate(ProviderKind.SVN) else ProviderState.AVAILABLE
            detail = (("the repository (/repo) rejected this user name and password. On some servers the repository login is "
                       "separate from the Polarion login: check by opening <server>/repo/ in a browser")
                      if state == ProviderState.AUTH_FAILED else plan.explain(ProviderKind.SVN))
            profile.providers.append(ProviderStatus(kind=ProviderKind.SVN, state=state, detail=detail))
            return state
        if resp.status_code == 200:
            s.svn = svn
            profile.providers.append(ProviderStatus(kind=ProviderKind.SVN, state=ProviderState.AUTHENTICATED,
                                                    detail="read-only access to /repo"))
            return ProviderState.AUTHENTICATED
        profile.providers.append(ProviderStatus(kind=ProviderKind.SVN, state=ProviderState.UNREACHABLE,
                                                detail=f"HTTP {resp.status_code}"))
        return ProviderState.UNREACHABLE

    # ------------------------------------------------------------------ discovery & verification
    def _discover_projects(self, s: Session, states: dict, profile: ServerProfile) -> None:
        errors = []
        if s.rest is not None and states.get(ProviderKind.REST) == ProviderState.AUTHENTICATED:
            try:
                profile.projects = discovery.projects_via_rest(s.rest)
                return
            except PavsError as exc:
                errors.append(f"REST: {exc}")
        if s.soap is not None:
            try:
                profile.projects = discovery.projects_via_soap(s.soap)
                return
            except PavsError as exc:
                errors.append(f"SOAP: {exc}")
        if errors:
            profile.warnings.append("Project discovery failed - " + "; ".join(errors))

    def _verify(self, s: Session, records: list[CapabilityRecord]) -> list[CapabilityRecord]:
        project = s.profile.projects[0].id if s.profile.projects else None
        user = s.profile.current_user.id if s.profile.current_user else None
        out = []
        for rec in records:
            cap = self.registry.get(rec.capability_id)
            strat = next((st for st in cap.strategies if rec.strategy and st.label == rec.strategy), None)
            if strat is None or not strat.probe or cap.write or rec.status not in (
                    CapabilityStatus.SUPPORTED, CapabilityStatus.PARTIAL):
                out.append(rec)
                continue
            probe = strat.probe
            if ("{projectId}" in probe and not project) or ("{userId}" in probe and not user):
                out.append(rec)
                continue
            probe = probe.replace("{projectId}", project or "").replace("{userId}", user or "")
            out.append(self._run_probe(s, rec, strat.provider, probe))
        return out

    def _run_probe(self, s: Session, rec: CapabilityRecord, provider: ProviderKind,
                   probe: str) -> CapabilityRecord:
        try:
            if provider == ProviderKind.REST and s.rest is not None:
                _, target = probe.split(None, 1)
                path, _, query = target.partition("?")
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                resp = s.rest.probe(path, params=params)
                code = resp.status_code
                if 200 <= code < 300:
                    return rec.model_copy(update={"verification": Verification.VERIFIED,
                                                  "detail": _join(rec.detail, f"verified: {probe} -> {code}")})
                if code in (401, 403):
                    return rec.model_copy(update={"status": CapabilityStatus.NEEDS_PERMISSION,
                                                  "verification": Verification.DETECTED,
                                                  "detail": _join(rec.detail, f"{probe} -> {code}")})
                return rec.model_copy(update={"verification": Verification.DETECTED,
                                              "detail": _join(rec.detail, f"probe {probe} -> HTTP {code}")})
            if provider == ProviderKind.SOAP and s.soap is not None:
                service, op = probe.split(".", 1)
                s.soap.call(service, op)
                return rec.model_copy(update={"verification": Verification.VERIFIED,
                                              "detail": _join(rec.detail, f"verified: {probe}")})
        except PolarionApiError as exc:
            status = CapabilityStatus.NEEDS_PERMISSION if exc.status in (401, 403) else rec.status
            return rec.model_copy(update={"status": status, "verification": Verification.DETECTED,
                                          "detail": _join(rec.detail, f"{probe} failed: {exc}")})
        except PavsError as exc:
            return rec.model_copy(update={"detail": _join(rec.detail, f"{probe} failed: {exc}")})
        return rec

    def _audit(self, env: Environment, profile: ServerProfile) -> None:
        if self.audit is None:
            return
        outcome = "ok" if profile.reachable and not profile.errors else "failed"
        self.audit.record("environment.connect", outcome, environment_id=env.id, target=env.base_url,
                          detail={"release": profile.version.release, "adapter": profile.adapter,
                                  "errors": profile.errors[:5]})


def _join(*parts: str) -> str:
    return " | ".join(p for p in parts if p)
