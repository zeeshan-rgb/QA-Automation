"""Role test for the UI: pick a Polarion user and roles, assign them in a test project, really do the work, report blockers.

Safety (each point is enforced here and covered by tests):
- needs the Lead application role, and the project must be an approved test project (the Write Guard checks it on every write);
- the only repository change is the one test user's entry in <project>/.polarion/security/user-roles.xml, and it is
  restored afterwards (the original text is kept in the local database, so a restore is possible even after a crash);
- every object created is titled "[PAVS-ROLE-<run>] ..." and recorded first; cleanup deletes only objects that still carry the tag;
- the test user's password is used in memory only and is never saved or logged.
"""
from __future__ import annotations

import getpass
import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..domain.errors import AuthenticationFailed, ConfigurationError, PavsError, WriteNotAuthorized
from ..domain.models import utcnow
from ..domain.testing import TestRunRecord
from ..engines.inspection.readers_roles import context_location
from ..engines.roletest.assign import Backup, RoleAssigner, find_svn, roles_in
from ..engines.roletest.expectations import Matrix, load_matrix, sheet_names
from ..engines.roletest.report import RoleTestReport, build_report
from ..engines.roletest.runner import Actor, RoleTestRunner, RunSetup, TypeSpec
from ..infra.redaction import SecretRegistry
from ..infra.storage import AuditLog, RoleTestRepository, TestRunRepository
from ..polarion.soap.client import SoapClient, as_list
from ..polarion.transport import HttpTransport
from ..polarion.svn.local import is_repository
from .connections import ConnectionService
from .roles import Action, RoleService

WAIT_FOR_ROLES_SECONDS = 60


@dataclass
class UserInfo:
    id: str
    name: str


@dataclass
class AssignCapability:
    automatic: bool
    reason: str = ""           # why not, in words the user can act on
    svn_exe: str | None = None
    folder_url: str | None = None


@dataclass
class RoleTestOutcome:
    report: RoleTestReport
    record: TestRunRecord
    roles_restored: bool | None      # None = roles were not changed by the app


class _DbLedger:
    def __init__(self, repo: RoleTestRepository, env_id: str, project: str, run_id: str) -> None:
        self.repo, self.env_id, self.project, self.run_id = repo, env_id, project, run_id

    def add(self, kind: str, uri: str, title: str) -> None:
        self.repo.add_artifact(self.run_id, self.env_id, self.project, kind, uri, title)

    def open_items(self) -> list[tuple[str, str, str]]:
        return [(k, u, t) for (_r, k, u, t) in self.repo.open_artifacts(self.env_id, self.project, self.run_id)]

    def done(self, uri: str) -> None:
        self.repo.done_artifact(self.env_id, self.project, uri)


class RoleTestService:
    def __init__(self, connections: ConnectionService, runs: TestRunRepository, audit: AuditLog, repo: RoleTestRepository,
                 http_transport=None, sleep=time.sleep) -> None:
        self.connections, self.runs, self.audit, self.repo = connections, runs, audit, repo
        self._http_transport = http_transport
        self._sleep = sleep
        self.roles: RoleService | None = None
        self.svn_run = None                          # tests inject a fake subprocess runner

    def _need(self, action: Action) -> None:
        if self.roles is not None:
            self.roles.require(action)

    # ------------------------------------------------------------------ what Polarion knows
    def _session(self, env_id: str):
        """The live session. If this window is not connected yet, sign in with the saved credentials (blocking: worker thread)."""
        s = self.connections.session(env_id)
        if s is None:
            creds = self.connections.environments.credentials(env_id)
            if creds is None or not (creds.password or creds.token):
                raise ConfigurationError("This window is not connected to the environment, and no sign-in is saved for it. Open 'Connect & Test', "
                                         "sign in as an administrator (username + password), then come back here.")
            self.connections.connect(self.connections.environments.get(env_id))
            s = self.connections.session(env_id)
        if s is None or s.soap is None:
            raise ConfigurationError("The sign-in did not give SOAP access. The role test needs SOAP: connect with a username and password "
                                     "(or a Personal Access Token that SOAP accepts) on 'Connect & Test'.")
        return s

    def users(self, env_id: str) -> list[UserInfo]:
        self._need(Action.RUN_TESTS)
        s = self._session(env_id)
        out = [UserInfo(str(u["id"]), str(u.get("name") or u["id"])) for u in as_list(s.soap.call("Project", "getUsers"))
               if isinstance(u, dict) and u.get("id")]
        return sorted(out, key=lambda u: u.id.lower())

    def location(self, env_id: str, project_id: str) -> str:
        s = self._session(env_id)
        proj = s.soap.call("Project", "getProject", {"projectID": project_id})
        body = proj.get("getProjectReturn", proj) if isinstance(proj, dict) else {}
        raw = body.get("location") if isinstance(body, dict) else None
        raw = raw.get("#text") if isinstance(raw, dict) else raw
        loc = context_location(raw)
        if not loc:
            raise ConfigurationError(f"The location of project '{project_id}' could not be read from the server.")
        return loc

    def project_roles(self, env_id: str, project_id: str) -> list[str]:
        self._need(Action.RUN_TESTS)
        s = self._session(env_id)
        loc = self.location(env_id, project_id)
        return sorted({r for r in as_list(s.soap.call("Security", "getContextRoles", {"location": loc})) if isinstance(r, str) and r})

    def work_item_types(self, env_id: str, project_id: str) -> list[TypeSpec]:
        self._need(Action.RUN_TESTS)
        s = self._session(env_id)
        opts = as_list(s.soap.call("Tracker", "getAllEnumOptionsForKey", {"projectId": project_id, "key": "type"}))
        return [TypeSpec(str(o["id"]), str(o.get("name") or o["id"])) for o in opts
                if isinstance(o, dict) and o.get("id") and str(o.get("hidden")) != "true"]

    def current_roles(self, env_id: str, project_id: str, user: str) -> list[str]:
        s = self._session(env_id)
        loc = self.location(env_id, project_id)
        return sorted(r for r in as_list(s.soap.call("Security", "getContextRolesForUser", {"userId": user, "location": loc})) if isinstance(r, str) and r)

    def document_spaces(self, env_id: str, project_id: str) -> list[str]:
        s = self._session(env_id)
        try:
            spaces = [x for x in as_list(s.soap.call("Tracker", "getDocumentSpaces", {"projectId": project_id})) if isinstance(x, str) and x]
        except PavsError:
            spaces = []
        return spaces or ["_default"]

    def sheet_names(self, path: str) -> list[str]:
        return sheet_names(path)

    def load_matrix(self, path: str, sheet: str | None = None) -> Matrix:
        return load_matrix(path, sheet)

    # ------------------------------------------------------------------ can the app assign roles by itself?
    def assign_capability(self, env_id: str, project_id: str) -> AssignCapability:
        s = self._session(env_id)
        env = s.environment
        creds = self.connections.environments.credentials(env_id)
        local = getattr(getattr(s, "svn", None), "repo", None) or env.local_repo_path
        if local and is_repository(local):
            base, hint, user, pw = Path(local).resolve().as_uri(), local, (creds.svn_user if creds else None) or env.username or "", None
        else:
            hint, user = None, ((creds.svn_user if creds else None) or env.username or "")
            pw = (creds.svn_password or creds.password) if creds else None
            base = env.server_root + "/repo"
            if not (user and pw):
                return AssignCapability(False, "The repository login is not known. Enter the repository user and password on the Connect page, "
                                               "or assign the role in Polarion yourself and choose 'The user already has the role'.")
        svn = find_svn(hint)
        if svn is None:
            return AssignCapability(False, "The Subversion command-line client (svn.exe) was not found. Install TortoiseSVN with the command-line tools "
                                           "or set PAVS_SVN; or assign the role in Polarion yourself and choose 'The user already has the role'.")
        try:
            path = self.location(env_id, project_id).split(":", 1)[1]
        except (IndexError, ConfigurationError) as exc:
            return AssignCapability(False, str(exc) or "The project folder could not be found.")
        return AssignCapability(True, "", svn, f"{base}{quote(path)}/.polarion/security")

    def _assigner(self, env_id: str, cap: AssignCapability) -> RoleAssigner:
        env = self.connections.environments.get(env_id)
        creds = self.connections.environments.credentials(env_id)
        local = bool(cap.folder_url and cap.folder_url.startswith("file:"))
        user = (creds.svn_user if creds else None) or env.username or ""
        pw = None if local else ((creds.svn_password or creds.password) if creds else None)
        return RoleAssigner(cap.svn_exe, cap.folder_url, user, pw, run=self.svn_run or subprocess.run)

    # ------------------------------------------------------------------ the run
    def run(self, env_id: str, project_id: str, user: str, password: str, roles: list[str], *, matrix: Matrix | None = None,
            assign: bool = True, types: list[TypeSpec] | None = None, restore: bool = True,
            areas: tuple[str, ...] = ("workitems", "documents", "other"), space: str | None = None, production_confirmation: str | None = None,
            progress=None) -> RoleTestOutcome:
        """Blocking: run it in a worker thread from the UI."""
        self._need(Action.WRITE_TESTS)
        report = progress or (lambda m, p: None)
        user, roles = user.strip(), [r for r in roles if r]
        if not user or not password:
            raise ConfigurationError("Choose the user and enter that user's password: the test signs in as that user.")
        if assign and not roles:
            raise ConfigurationError("Choose at least one role to give the user.")
        self._session(env_id)
        session = self.connections.sync_environment(env_id)
        env = session.environment
        if project_id not in env.test_projects:
            raise WriteNotAuthorized(f"Project '{project_id}' has not been approved for test data. Approve it first.")
        if matrix is not None and roles and not any(matrix.has_role(r) for r in roles):
            raise ConfigurationError(f"None of the chosen roles is in the sheet '{matrix.sheet}' (it has: {', '.join(matrix.roles)}).")
        SecretRegistry.register(password)
        loc = self.location(env_id, project_id)
        all_types = types or self.work_item_types(env_id, project_id)
        if not all_types:
            raise ConfigurationError("The project has no Work Item types to test.")
        space = space or self.document_spaces(env_id, project_id)[0]

        run_id = uuid.uuid4().hex[:8]
        auth = session.guard.issue(project_id, run_id, _user(), production_confirmation=production_confirmation)
        self.audit.record("roletest.start", "ok", environment_id=env_id, target=project_id,
                          detail={"run": run_id, "user": user, "roles": roles, "assign": assign})
        backup: Backup | None = None
        assigner: RoleAssigner | None = None
        restored: bool | None = None
        user_soap = None
        user_transport = None
        notes: list[str] = []
        probes: list = []
        try:
            if assign:
                cap = self.assign_capability(env_id, project_id)
                if not cap.automatic:
                    raise ConfigurationError(cap.reason)
                report("Assigning the role(s) to the user", 2)
                assigner = self._assigner(env_id, cap)
                backup = assigner.assign(user, roles)
                self.repo.set_pending(run_id, env_id, project_id, json.dumps(backup.to_dict()))
                self.audit.record("roletest.assign", "ok", environment_id=env_id, target=project_id, detail={"run": run_id, "user": user, "roles": roles})
                self._wait_for_roles(env_id, project_id, user, roles, report)
            else:
                roles = self.current_roles(env_id, project_id, user)
                if not roles:
                    raise ConfigurationError(f"'{user}' has no role in project '{project_id}'. Assign a role in Polarion first, or let the app assign one.")
            report("Signing in as the test user", 12)
            user_transport = HttpTransport(env.tls, env.timeout_seconds, session.guard, transport=self._http_transport)
            user_soap = SoapClient(user_transport, env.base_url)
            try:
                user_soap.login_password(user, password)
            except AuthenticationFailed:
                raise ConfigurationError(f"Signing in as '{user}' failed: check the password.") from None
            user_soap.set_reauth(lambda: user_soap.login_password(user, password))
            tag = f"[PAVS-ROLE-{run_id}]"
            runner = RoleTestRunner(Actor(session.soap, env.username or "admin", project_id, auth), Actor(user_soap, user, project_id, auth),
                                    RunSetup(project_id, user, tag, all_types, space, areas, ", ".join(roles)),
                                    _DbLedger(self.repo, env_id, project_id, run_id), progress=lambda m, p: report(m, 15 + int(p * 0.75)))
            probes = runner.run()
            notes += runner.notes
        finally:
            session.guard.revoke(auth)
            if user_soap is not None:
                try:
                    user_soap.logout()
                except PavsError:
                    pass
            if user_transport is not None:
                user_transport.close()
            if backup is not None and assigner is not None:
                restored = False
                if restore:
                    report("Restoring the user's original roles", 96)
                    try:
                        assigner.restore(backup)
                        self.repo.clear_pending(run_id)
                        self._wait_for_roles(env_id, project_id, user, roles_in(backup.original, user), report, raise_on_timeout=False, seconds=30)
                        restored = True
                        self.audit.record("roletest.restore", "ok", environment_id=env_id, target=project_id, detail={"run": run_id, "user": user})
                    except PavsError as exc:
                        notes.append(f"THE ROLES OF '{user}' WERE NOT RESTORED ({exc}). Use 'Restore roles' in the Role Test page, or restore user-roles.xml by hand.")
                        self.audit.record("roletest.restore", "failed", environment_id=env_id, target=project_id, detail={"run": run_id, "error": str(exc)[:200]})
                else:
                    notes.append(f"'{user}' was left with the test role(s) as requested; 'Restore roles' puts the original back.")
        rep = build_report(user, roles, project_id, probes, all_types, matrix, notes)
        rec = TestRunRecord(environment_id=env_id, environment_name=env.name, server_url=env.base_url, project_id=project_id,
                            release=session.profile.version.release, adapter="role test", kind="role_test",
                            requirement_set=f"{user} as {', '.join(roles)}", user=_user())
        rec.results, rec.finished_at = rep.results, utcnow()
        rec.inspection_gaps = [n for n in notes]
        self.runs.add(rec)
        self.audit.record("roletest.finish", "passed" if rep.passed else "blockers", environment_id=env_id, target=project_id,
                          detail={"run": run_id, "user": user, "roles": roles, "counts": rep.counts(), "restored": restored})
        report("Done", 100)
        return RoleTestOutcome(rep, rec, restored)

    def _wait_for_roles(self, env_id: str, project_id: str, user: str, roles: list[str], report, raise_on_timeout: bool = True,
                        seconds: int = WAIT_FOR_ROLES_SECONDS) -> None:
        deadline = time.monotonic() + seconds
        have: list[str] = []
        while True:
            have = self.current_roles(env_id, project_id, user)
            if sorted(have) == sorted(roles):
                return
            if time.monotonic() >= deadline:
                if not raise_on_timeout:
                    return
                raise PavsError(f"The server still shows the roles {have or 'none'} for '{user}' after {seconds} s. "
                                "The change was committed; it will be undone.")
            report("Waiting for Polarion to pick up the new role(s)", 8)
            self._sleep(2)

    # ------------------------------------------------------------------ recovery
    def pending_restores(self, env_id: str | None = None) -> list[tuple[str, str, str]]:
        """(run_id, project_id, user) of role changes that were not restored yet."""
        return [(rid, pid, json.loads(d)["user"]) for rid, _e, pid, d in self.repo.pending(env_id)]

    def restore_pending(self, env_id: str) -> int:
        """Put back the roles of every unfinished role test of this environment. Returns how many were restored."""
        self._need(Action.WRITE_TESTS)
        n = 0
        for rid, _e, pid, data in self.repo.pending(env_id):
            b = Backup.from_dict(json.loads(data))
            cap = AssignCapability(True, "", find_svn(None), b.folder_url)
            if cap.svn_exe is None:
                local = self.connections.environments.get(env_id).local_repo_path
                cap.svn_exe = find_svn(local)
            if cap.svn_exe is None:
                raise ConfigurationError("The Subversion command-line client (svn.exe) was not found; the roles cannot be restored from here.")
            self._assigner(env_id, cap).restore(b)
            self.repo.clear_pending(rid)
            self.audit.record("roletest.restore", "ok", environment_id=env_id, target=pid, detail={"run": rid, "user": b.user, "late": True})
            n += 1
        return n

    def leftovers(self, env_id: str, project_id: str) -> int:
        return len(self.repo.open_artifacts(env_id, project_id))

    def cleanup(self, env_id: str, project_id: str, production_confirmation: str | None = None) -> tuple[int, list[str]]:
        """Retry deleting test objects an earlier role test left behind. Only objects this tool recorded and tagged."""
        self._need(Action.WRITE_TESTS)
        session = self._session(env_id)
        items = self.repo.open_artifacts(env_id, project_id)
        if not items:
            return 0, []
        auth = session.guard.issue(project_id, "cleanup", _user(), production_confirmation=production_confirmation)
        deleted, left = 0, []
        try:
            for run_id in dict.fromkeys(r for r, *_ in items):
                runner = RoleTestRunner(Actor(session.soap, "admin", project_id, auth), Actor(session.soap, "admin", project_id, auth),
                                        RunSetup(project_id, "", f"[PAVS-ROLE-{run_id}]", []), _DbLedger(self.repo, env_id, project_id, run_id))
                d, lf = runner.cleanup()
                deleted += d
                left += lf
        finally:
            session.guard.revoke(auth)
        self.audit.record("roletest.cleanup", "ok" if not left else "leftovers", environment_id=env_id, target=project_id, detail={"deleted": deleted, "left": len(left)})
        return deleted, left


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "unknown"
