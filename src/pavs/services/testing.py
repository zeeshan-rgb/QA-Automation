"""Requirements, test generation and test runs for the UI. Read-only against Polarion, no AI."""
from __future__ import annotations

import getpass
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.config import ConfigSnapshot
from ..domain.errors import ConfigurationError, PavsError, WriteNotAuthorized
from ..domain.enums import TestStatus
from ..domain.models import utcnow
from ..domain.requirements import (Finding, Requirement, RequirementSet, TestCase, TestCaseStatus)
from ..domain.testing import CheckResult, Severity, TestRunRecord
from ..engines.inspection import InspectionContext, check_permission
from ..engines.requirements.importers import ImportResult, import_file, import_text
from ..engines.requirements.rules import Names, find_conflicts, generate_for_requirement
from ..engines.analysis import sources as script_sources
from ..engines.analysis.results import analyze_to_results
from ..engines.testing.behaviour import Account, Behaviour, cleanup_artifacts
from ..engines.testing.data import load_items
from ..engines.testing.runner import new_record, run_checks
from ..engines.testing.view import ConfigView
from ..infra.storage import ArtifactRepository, AuditLog, RequirementRepository, TestRunRepository
from ..reporting.exporters import export_run
from .connections import ConnectionService
from .inspection import InspectionService
from .roles import Action, RoleService


@dataclass
class Analysis:
    requirement_set: RequirementSet
    requirements: list[Requirement]
    cases: list[TestCase]
    findings: list[Finding]
    warnings: list[str] = field(default_factory=list)


class TestingService:
    __test__ = False

    def __init__(self, connections: ConnectionService, inspection: InspectionService,
                 requirements: RequirementRepository, runs: TestRunRepository, audit: AuditLog,
                 artifacts: ArtifactRepository | None = None) -> None:
        self.artifacts = artifacts
        self.connections = connections
        self.inspection = inspection
        self.requirements = requirements
        self.runs = runs
        self.audit = audit
        self.roles: RoleService | None = None

    def _need(self, action: Action) -> None:
        if self.roles is not None:
            self.roles.require(action)

    # ------------------------------------------------------------------ requirements
    def import_path(self, path: str | Path, env_id: str | None = None, project_id: str | None = None) -> Analysis:
        self._need(Action.RUN_TESTS)
        res = import_file(path)
        return self._store(res, env_id, project_id)

    def import_pasted(self, text: str, name: str = "Pasted requirements", env_id: str | None = None,
                      project_id: str | None = None) -> Analysis:
        self._need(Action.RUN_TESTS)
        res = import_text(text, name)
        return self._store(res, env_id, project_id)

    def _store(self, res: ImportResult, env_id, project_id) -> Analysis:
        if not res.requirements:
            raise ConfigurationError(" ".join(res.warnings) or "No requirements were found in the input.")
        self.requirements.add_set(res.requirement_set, res.requirements, res.test_cases)
        self.audit.record("requirements.import", "ok", target=res.requirement_set.name,
                          detail={"count": len(res.requirements), "format": res.requirement_set.source_format})
        out = self.analyze(res.requirement_set.id, env_id, project_id)
        out.warnings = res.warnings + out.warnings
        return out

    def sets(self) -> list[RequirementSet]:
        return self.requirements.list_sets()

    def delete_set(self, set_id: str) -> None:
        self.requirements.delete_set(set_id)

    def load(self, set_id: str) -> Analysis:
        rs = self.requirements.get_set(set_id)
        if rs is None:
            raise ConfigurationError("Requirement set not found.")
        return Analysis(rs, self.requirements.requirements(set_id), self.requirements.cases(set_id),
                        self.requirements.findings(set_id))

    def _names(self, env_id: str | None, project_id: str | None, snap: ConfigSnapshot | None = None) -> Names:
        if snap is None and env_id and project_id:
            history = self.inspection.history(env_id, project_id)
            snap = self.inspection.load(history[0][0]) if history else None
        return Names.from_view(ConfigView(snap)) if snap else Names()

    def analyze(self, set_id: str, env_id: str | None = None, project_id: str | None = None,
                snap: ConfigSnapshot | None = None) -> Analysis:
        """(Re)generate rule-based test cases + findings. Cases the user rejected stay rejected."""
        rs = self.requirements.get_set(set_id)
        if rs is None:
            raise ConfigurationError("Requirement set not found.")
        reqs = self.requirements.requirements(set_id)
        names = self._names(env_id, project_id, snap)
        old = {self._key(c): c for c in self.requirements.cases(set_id)}
        cases: list[TestCase] = []
        findings: list[Finding] = []
        per_req: dict[str, list[TestCase]] = {}
        for r in reqs:
            g = generate_for_requirement(r, names)
            for c in g.cases:
                prev = old.get(self._key(c))
                if prev is not None:
                    c.id, c.status, c.reviewed_by, c.reviewed_at = prev.id, prev.status, prev.reviewed_by, prev.reviewed_at
                cases.append(c)
            per_req[r.id] = g.cases
            findings += g.findings
        findings += find_conflicts(per_req, {r.id: r.label for r in reqs})
        self.requirements.replace_generated(set_id, cases, findings)
        return Analysis(rs, reqs, self.requirements.cases(set_id), findings)

    @staticmethod
    def _key(c: TestCase) -> str:
        return f"{c.requirement_id}|{c.check.model_dump_json(exclude={'note'})}"

    def set_case_enabled(self, case_id: str, set_id: str, enabled: bool) -> None:
        for c in self.requirements.cases(set_id):
            if c.id == case_id:
                c.status = TestCaseStatus.APPROVED if enabled else TestCaseStatus.REJECTED
                c.reviewed_by, c.reviewed_at = _user(), utcnow()
                self.requirements.update_case(c)
                return

    # ------------------------------------------------------------------ running
    def run(self, env_id: str, project_id: str, *, health: bool = True, set_id: str | None = None,
            progress=None, data: bool = False, behavioural: bool = False,
            production_confirmation: str | None = None, scripts: bool = False,
            script_folder: str | None = None, browser: bool = False) -> TestRunRecord:
        """Inspect the project (read-only), then run the selected checks. Blocking: use a worker thread."""
        self._need(Action.WRITE_TESTS if behavioural else Action.RUN_TESTS)
        if not health and not set_id and not data and not scripts and not browser:
            raise ConfigurationError("Choose at least one thing to test: health checks, Work Item data checks, script analysis, browser checks or a requirement set.")
        report = progress or (lambda m, p: None)
        scaled = lambda lo, hi: (lambda m, p: report(m, lo + int((hi - lo) * p / 100)))  # noqa: E731
        snap_id, snap = self.inspection.inspect(env_id, project_id, progress=scaled(0, 60))
        session = self.connections.session(env_id)
        env_name = session.environment.name if session else ""
        url = session.environment.base_url if session else ""

        cases: list[TestCase] = []
        reqs: dict[str, Requirement] = {}
        rs_name = ""
        if set_id:
            report("Generating tests from the requirements", 62)
            analysis = self.analyze(set_id, env_id, project_id, snap=snap)
            cases, rs_name = analysis.cases, analysis.requirement_set.name
            reqs = {r.id: r for r in analysis.requirements}

        server_perm = None
        soap_ok = bool(session and session.soap is not None)
        need_data = data or any(c.check.kind.value == "traceability" for c in cases)
        dataset = None
        data_note = None
        if need_data and session is not None:
            report("Reading the Work Items", 63)
            try:
                dataset = load_items(session, project_id)
            except PavsError as exc:
                data_note = f"Work Items could not be read: {exc}"
        if soap_ok:
            ictx = InspectionContext(session=session, project_id=project_id)
            server_perm = lambda user, perm: check_permission(ictx, user, perm).granted  # noqa: E731

        kind = "all" if health and set_id else "health" if health else "requirements"
        record = new_record(snap, env_name, url, kind, _user(), rs_name, snap_id)
        if data_note:
            record.inspection_gaps.append(data_note)
        user_id = session.profile.current_user.id if session and session.profile.current_user else None
        behaviour, auth, extra = None, None, []
        if behavioural:
            behaviour, auth, extra = self._prepare_behaviour(session, snap, env_id, project_id, record.id, production_confirmation)
        try:
            record.results = run_checks(snap, health=health, cases=cases, requirements=reqs,
                                        server_permission=server_perm, soap_available=soap_ok, progress=scaled(60, 99),
                                        dataset=dataset, behaviour=behaviour, user_id=user_id, data_checks=data)
        finally:
            if behaviour is not None:
                extra.append(self._finish_behaviour(session, behaviour, auth, env_id, project_id))
        record.results += extra
        if scripts:
            report("Analysing scripts and templates", 97)
            record.results += self._analyze_scripts(session, project_id, snap.release, script_folder)
        if browser:
            report("Browser checks", 98)
            record.results += self._browser_checks(session, project_id)
        record.finished_at = utcnow()
        self.runs.add(record)
        self.audit.record("tests.run", record.verdict.lower().replace(" ", "_"), environment_id=env_id,
                          target=project_id, detail={"kind": kind, "counts": record.counts(), "behavioural": behavioural,
                                                     "test_items_created": len(behaviour.created) if behaviour else 0})
        report("Done", 100)
        return record

    # ------------------------------------------------------------------ browser checks (opt-in, navigation only)
    def _browser_checks(self, session, project_id: str) -> list[CheckResult]:
        from ..engines.testing.browser import run_browser_checks
        if session is None:
            return run_browser_checks("", project_id, None, None)
        env = session.environment
        creds = self.connections.environments.credentials(env.id)
        password = creds.password if creds else None
        return run_browser_checks(env.base_url, project_id, env.username if password else None, password, env.tls.verify)

    # ------------------------------------------------------------------ script and template analysis (read-only)
    def _analyze_scripts(self, session, project_id: str, release: str | None, folder: str | None) -> list[CheckResult]:
        out: list[CheckResult] = []
        files = []
        label = []
        if session is not None and session.svn is not None:
            from ..engines.inspection.repo import RepoConfig
            try:
                repo_files = script_sources.from_repo(RepoConfig(session.svn), project_id)
                files += repo_files
                label.append(f"repository ({len(repo_files)} items)")
            except PavsError as exc:
                out.append(CheckResult(check_id="S00", group="Scripts", name="Repository templates", status=TestStatus.BLOCKED,
                                       expected="templates read from the repository", actual="could not be read", evidence=[str(exc)]))
        if folder:
            try:
                local = script_sources.from_folder(folder)
                files += local
                label.append(f"folder {folder} ({len(local)} files)")
            except PavsError as exc:
                out.append(CheckResult(check_id="S00", group="Scripts", name="Script folder", status=TestStatus.BLOCKED,
                                       expected="scripts read from the folder", actual="could not be read", evidence=[str(exc)]))
        return out + analyze_to_results(files, release, " + ".join(label) or "no source")

    def analyze_local_scripts(self, folder: str, release: str | None = None) -> TestRunRecord:
        """Offline: analyse a folder of scripts/templates without any Polarion connection. Saved to history."""
        self._need(Action.RUN_TESTS)
        results = analyze_to_results(script_sources.from_folder(folder), release, f"folder {folder}")
        rec = TestRunRecord(environment_id="local", environment_name="(local files)", server_url=folder, project_id="(local scripts)",
                            release=release, adapter="static analysis", kind="scripts", user=_user())
        rec.results, rec.finished_at = results, utcnow()
        self.runs.add(rec)
        self.audit.record("scripts.analyze_local", rec.verdict.lower().replace(" ", "_"), target=folder,
                          detail={"counts": rec.counts()})
        return rec

    # ------------------------------------------------------------------ behavioural tests (writes, test projects only)
    def _prepare_behaviour(self, session, snap, env_id, project_id, run_id, production_confirmation):
        """-> (Behaviour | None, WriteAuthorization | None, [extra results]). Raises WriteNotAuthorized on a refused approval."""
        session = self.connections.sync_environment(env_id) or session
        env = session.environment
        if self.artifacts is None:
            raise ConfigurationError("Behavioural tests are not available in this build.")
        if project_id not in env.test_projects:
            raise WriteNotAuthorized(f"Project '{project_id}' has not been approved for test data. "
                                     "Approve it first (the app asks you to confirm what will be created).")
        auth = session.guard.issue(project_id, run_id, _user(), production_confirmation=production_confirmation)
        accounts: dict[str, Account] = {}
        for role, user in env.test_accounts.items():
            token = self.connections.environments.test_account_token(env_id, user)
            if token:
                accounts[role] = Account(role, user, token)
        beh = Behaviour(session=session, project=project_id, run_id=run_id, env_id=env_id, auth=auth,
                        view=ConfigView(snap), accounts=accounts, artifacts=self.artifacts)
        ok, why = beh.available()
        if not ok:
            session.guard.revoke(auth)
            return None, None, [CheckResult(check_id="B00", group="Behavioural", name="Behavioural tests",
                                            status=TestStatus.BLOCKED, expected="tests that really try the actions",
                                            actual="not run", evidence=[why], mechanism="-")]
        self.audit.record("behavioural.start", "ok", environment_id=env_id, target=project_id,
                          detail={"run": run_id, "accounts": sorted(accounts)})
        return beh, auth, []

    def _finish_behaviour(self, session, beh, auth, env_id, project_id) -> CheckResult:
        """Always delete the test Work Items this run created, then report it as a result of its own."""
        try:
            deleted, leftovers, log = beh.cleanup()
        except Exception as exc:     # never let cleanup problems hide the results
            deleted, leftovers, log = 0, [a.ref for a in beh.created if a.deleted_at is None], [f"cleanup failed: {exc}"]
        session.guard.revoke(auth)
        self.audit.record("behavioural.cleanup", "ok" if not leftovers else "leftovers", environment_id=env_id,
                          target=project_id, detail={"created": len(beh.created), "deleted": deleted, "leftovers": leftovers})
        ok = not leftovers
        return CheckResult(
            check_id="B99", group="Behavioural", name="Cleanup of the test Work Items",
            status=TestStatus.PASS if ok else TestStatus.FAIL, severity=Severity.ERROR if not ok else Severity.INFO,
            expected="every test Work Item created by this run is deleted again",
            actual=(f"{len(beh.created)} created, all removed" if ok else f"{len(leftovers)} could not be removed: {', '.join(leftovers)}"),
            mechanism="REST DELETE (guarded)",
            evidence=log + ([] if ok else ["Use 'Clean up test data' to retry. Nothing outside the test tag is ever deleted."]))

    def leftover_artifacts(self, env_id: str, project_id: str | None = None) -> list:
        return self.artifacts.open_items(env_id, project_id) if self.artifacts else []

    def cleanup(self, env_id: str, project_id: str, production_confirmation: str | None = None) -> tuple[int, list[str], list[str]]:
        """Retry deleting test Work Items an earlier run left behind. Only items this tool recorded and tagged."""
        self._need(Action.WRITE_TESTS)
        session = self.connections.sync_environment(env_id)
        if session is None:
            raise ConfigurationError("Connect to the environment first.")
        items = self.artifacts.open_items(env_id, project_id) if self.artifacts else []
        if not items:
            return 0, [], ["Nothing to clean up."]
        auth = session.guard.issue(project_id, "cleanup", _user(), production_confirmation=production_confirmation)
        try:
            result = cleanup_artifacts(session, self.artifacts, auth, project_id, env_id, items)
        finally:
            session.guard.revoke(auth)
        self.audit.record("behavioural.cleanup", "ok" if not result[1] else "leftovers", environment_id=env_id,
                          target=project_id, detail={"deleted": result[0], "leftovers": result[1], "manual": True})
        return result

    def previous_run(self, rec: TestRunRecord) -> TestRunRecord | None:
        """The run before `rec` for the same environment, project and kind (the baseline for regression comparison)."""
        for r in self.runs.list(rec.environment_id, rec.project_id):
            if r.id != rec.id and r.kind == rec.kind and r.started_at < rec.started_at:
                return r
        return None

    def compare_runs(self, old_id: str, new_id: str):
        from .regression import diff_runs
        old, new = self.runs.get(old_id), self.runs.get(new_id)
        if old is None or new is None:
            raise ConfigurationError("Test run not found.")
        return diff_runs(old, new)

    def history(self, env_id: str | None = None, project_id: str | None = None) -> list[TestRunRecord]:
        return self.runs.list(env_id, project_id)

    def get_run(self, run_id: str) -> TestRunRecord | None:
        return self.runs.get(run_id)

    def export(self, run_id: str, path: str | Path) -> Path:
        run = self.runs.get(run_id)
        if run is None:
            raise ConfigurationError("Test run not found.")
        out = export_run(run, path)
        self.audit.record("report.export", "ok", environment_id=run.environment_id, target=str(out.name))
        return out


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "unknown"
