"""Merge preparation for projects and templates, with TortoiseSVN for the actual merge.

Read-only here: the two configuration trees (`<project>/.polarion`) are compared through the repository reader, the
files that would arrive in the target are checked (well-formed XML, script and Velocity rules against the TARGET
release), and the result is saved as a report. The real merge, with history, 3-way logic and conflict resolution, is
done in TortoiseSVN, which this service starts on request. The tool never commits anything.

Limit: this is a two-way comparison (source now vs target now). It cannot know the common ancestor, so it shows what
differs, not what TortoiseSVN will report as conflicts.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.enums import EnvironmentKind, TestStatus
from ..domain.errors import ConfigurationError, PavsError
from ..domain.models import utcnow
from ..domain.testing import CheckResult, Severity, TestRunRecord
from ..engines.analysis import sources as script_sources
from ..engines.analysis.analyzer import SourceFile, analyze_files, release_code
from ..engines.inspection.repo import RepoConfig
from ..infra import safexml
from ..infra.storage import AuditLog, TestRunRepository
from ..integrations.tortoisesvn import TortoiseLauncher
from .connections import ConnectionService
from .roles import Action, RoleService

TEXT_SUFFIXES = (".xml", ".vm", ".js", ".txt", ".json", ".properties", ".html", ".css", ".xsl", ".xslt")
MAX_FILES = 900
MAX_DEPTH = 12


@dataclass
class MergeFinding:
    rule: str
    severity: str           # error | warning | info
    line: int
    message: str


@dataclass
class FileDiff:
    path: str                        # relative to <project>/.polarion
    area: str                        # first folder: tracker, pages, security ...
    state: str                       # added | removed | changed | same
    findings: list[MergeFinding] = field(default_factory=list)
    source_url: str = ""
    target_url: str = ""

    @property
    def worst(self) -> str:
        order = ["error", "warning", "info"]
        return min((f.severity for f in self.findings), key=order.index, default="")


@dataclass
class MergeReport:
    source_label: str
    target_label: str
    source_env_id: str
    target_env_id: str
    source_project: str
    target_project: str
    source_release: str | None
    target_release: str | None
    source_root_url: str
    target_root_url: str
    files: list[FileDiff] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    run_id: str = ""

    def counts(self) -> dict[str, int]:
        out = {"added": 0, "removed": 0, "changed": 0, "same": 0}
        for f in self.files:
            out[f.state] += 1
        return out

    @property
    def blockers(self) -> list[tuple[FileDiff, MergeFinding]]:
        return [(f, x) for f in self.files if f.state in ("added", "changed") for x in f.findings if x.severity == "error"]

    @property
    def ready(self) -> bool:
        return not self.blockers


class MergeService:
    def __init__(self, connections: ConnectionService, runs: TestRunRepository, audit: AuditLog,
                 launcher: TortoiseLauncher | None = None) -> None:
        self.connections = connections
        self.runs = runs
        self.audit = audit
        self.launcher = launcher or TortoiseLauncher()
        self.roles: RoleService | None = None

    def _need(self, action: Action) -> None:
        if self.roles is not None:
            self.roles.require(action)

    # ------------------------------------------------------------------ compare (read-only)
    def compare(self, source_env_id: str, source_project: str, target_env_id: str, target_project: str,
                progress=None, include_same: bool = False) -> MergeReport:
        """Blocking (HTTP): run it in a worker thread. Reads the repository only."""
        self._need(Action.RUN_TESTS)
        report = progress or (lambda m, p: None)
        src, dst = self._side(source_env_id, source_project), self._side(target_env_id, target_project)
        rep = MergeReport(
            source_label=f"{src['env'].name}: {source_project}", target_label=f"{dst['env'].name}: {target_project}",
            source_env_id=source_env_id, target_env_id=target_env_id, source_project=source_project,
            target_project=target_project, source_release=src["release"], target_release=dst["release"],
            source_root_url=src["svn"].url(src["root"]), target_root_url=dst["svn"].url(dst["root"]))
        if source_env_id == target_env_id and source_project == target_project:
            raise ConfigurationError("Source and target are the same project. Choose two different ones.")

        report("Listing the source configuration", 5)
        src_files, trunc1 = self._walk(src["repo"], src["root"])
        report("Listing the target configuration", 15)
        dst_files, trunc2 = self._walk(dst["repo"], dst["root"])
        if trunc1 or trunc2:
            rep.notes.append(f"Only the first {MAX_FILES} files of a project were compared.")
        names = sorted(set(src_files) | set(dst_files))
        total = max(1, len(names))
        for i, rel in enumerate(names):
            report(f"Comparing {rel}", 20 + int(60 * i / total))
            a = src["repo"].read(src_files[rel]) if rel in src_files else None
            b = dst["repo"].read(dst_files[rel]) if rel in dst_files else None
            if a is None and b is None:
                continue
            state = "added" if b is None else "removed" if a is None else "same" if _norm(a) == _norm(b) else "changed"
            if state == "same" and not include_same:
                continue
            fd = FileDiff(rel, rel.split("/", 1)[0] if "/" in rel else "(root)", state,
                          source_url=src["svn"].url(src_files[rel]) if rel in src_files else "",
                          target_url=dst["svn"].url(dst_files[rel]) if rel in dst_files else "")
            if state in ("added", "changed") and a is not None:
                fd.findings = self._check_incoming(rel, a, dst["release"])
            rep.files.append(fd)
        self._release_notes(rep)
        report("Saving the report", 90)
        rep.run_id = self._save(rep)
        self.audit.record("merge.compare", "ready" if rep.ready else "blocked", environment_id=target_env_id,
                          target=f"{source_project}->{target_project}", detail=rep.counts())
        report("Done", 100)
        return rep

    def _side(self, env_id: str, project_id: str) -> dict:
        session = self.connections.session(env_id)
        if session is None:
            self.connections.connect(self.connections.environments.get(env_id))
            session = self.connections.session(env_id)
        if session is None:
            raise ConfigurationError("Could not connect to the environment.")
        if session.svn is None:
            raise ConfigurationError(f"'{session.environment.name}': the repository reader is off. Connect with the password and "
                                     "'Read configuration files' ticked (it is read-only).")
        repo = RepoConfig(session.svn)
        root = repo.project_root(project_id)
        if root is None:
            raise ConfigurationError(f"Project '{project_id}' was not found in the repository of '{session.environment.name}'.")
        return {"env": session.environment, "svn": session.svn, "repo": repo, "root": root,
                "release": session.profile.version.release}

    @staticmethod
    def _walk(repo: RepoConfig, root: str) -> tuple[dict[str, str], bool]:
        """{relative path: repository path} for the text files below `root`."""
        found: dict[str, str] = {}
        stack = [(root, 0)]
        truncated = False
        while stack:
            folder, depth = stack.pop()
            for name in repo.list(folder):
                if name.startswith(".svn") or name in ("../", "./"):
                    continue
                full = posixpath.join(folder, name.rstrip("/"))
                if name.endswith("/"):
                    if depth < MAX_DEPTH:
                        stack.append((full, depth + 1))
                elif name.lower().endswith(TEXT_SUFFIXES):
                    if len(found) >= MAX_FILES:
                        truncated = True
                        break
                    found[full[len(root):].lstrip("/")] = full
        return found, truncated

    @staticmethod
    def _check_incoming(rel: str, text: str, target_release: str | None) -> list[MergeFinding]:
        out: list[MergeFinding] = []
        lower = rel.lower()
        if lower.endswith((".xml", ".xsl", ".xslt")):
            try:
                safexml.fromstring(text)
            except safexml.ParseError as exc:
                line = exc.position[0] if getattr(exc, "position", None) else 1
                out.append(MergeFinding("M01", "error", line, f"not well-formed XML: {exc}"))
        files: list[SourceFile] = []
        kind = script_sources.kind_of(rel)
        if kind:
            files.append(SourceFile(rel, kind, text, "repo"))
        elif lower.endswith("page.xml"):
            files += [SourceFile(f"{rel} ({label})", "vm", code, "repo") for label, code in script_sources.velocity_from_page_xml(text)]
        if files:
            findings, _ = analyze_files(files, target_release, check_syntax=False)
            out += [MergeFinding(f.rule, f.severity, f.line, f.message) for f in findings if f.rule != "S14"]
        return out

    @staticmethod
    def _release_notes(rep: MergeReport) -> None:
        s, t = release_code(rep.source_release), release_code(rep.target_release)
        if s and t and s > t:
            rep.notes.append(f"The source is Polarion {s}, the target is older ({t}). Newer features and API calls may not exist there: "
                             "the script rules above are checked against the target release.")
        elif not t:
            rep.notes.append("The target release is unknown, so release-specific script checks (S04) were skipped.")
        rep.notes.append("Two-way comparison: it shows what differs now. TortoiseSVN's merge uses the history and finds the real conflicts.")

    # ------------------------------------------------------------------ report (reuses the normal exports)
    def _save(self, rep: MergeReport) -> str:
        env = self.connections.environments.get(rep.target_env_id)
        results: list[CheckResult] = []
        for i, f in enumerate(f for f in rep.files if f.state != "same"):
            sev = f.worst
            if f.state == "removed":
                status, actual = TestStatus.NOT_TESTABLE, "only in the target: a merge from the source keeps it"
            elif sev == "error":
                status, actual = TestStatus.FAIL, f"{f.state}; would break after the merge"
            elif sev == "warning":
                status, actual = TestStatus.PARTIAL_PASS, f"{f.state}; warnings"
            else:
                status, actual = TestStatus.PASS, f"{f.state}; no problems found"
            results.append(CheckResult(
                check_id=f"M{i + 1:03d}", group="Merge check", name=f.path, status=status,
                severity=Severity.ERROR if sev == "error" else Severity.WARNING if sev == "warning" else Severity.INFO,
                expected="incoming file is valid on the target release", actual=actual, mechanism="repository compare (read-only)",
                evidence=[f"{x.rule} line {x.line}: {x.message}" for x in f.findings[:12]]))
        if not results:
            results.append(CheckResult(check_id="M000", group="Merge check", name="Differences", status=TestStatus.PASS,
                                       expected="differences to review", actual="the two configurations are identical",
                                       mechanism="repository compare (read-only)"))
        rec = TestRunRecord(environment_id=rep.target_env_id, environment_name=rep.target_label, server_url=env.base_url,
                            project_id=f"{rep.source_project} -> {rep.target_project}", release=rep.target_release,
                            adapter="merge check", kind="merge", user=_user(), results=results, finished_at=utcnow(),
                            inspection_gaps=list(rep.notes))
        self.runs.add(rec)
        return rec.id

    # ------------------------------------------------------------------ TortoiseSVN hand-offs
    @property
    def tortoise_available(self) -> bool:
        return self.launcher.available

    def _log(self, what: str, **detail) -> None:
        self.audit.record("tortoisesvn." + what, "started", detail=detail)

    def open_compare(self, rep: MergeReport) -> None:
        self._need(Action.RUN_TESTS)
        self.launcher.compare_urls(rep.source_root_url, rep.target_root_url)
        self._log("compare", source=rep.source_root_url, target=rep.target_root_url)

    def open_file_diff(self, rep: MergeReport, fd: FileDiff) -> None:
        self._need(Action.RUN_TESTS)
        if fd.source_url and fd.target_url:
            self.launcher.diff_urls(fd.source_url, fd.target_url)
        else:
            self.launcher.log(fd.source_url or fd.target_url)
        self._log("diff", file=fd.path)

    def open_repo_browser(self, url: str) -> None:
        self._need(Action.RUN_TESTS)
        self.launcher.repo_browser(url)
        self._log("repobrowser", url=url)

    def open_log(self, url: str) -> None:
        self._need(Action.RUN_TESTS)
        self.launcher.log(url)
        self._log("log", url=url)

    def checkout_target(self, rep: MergeReport, folder: str | Path) -> None:
        """Start the TortoiseSVN checkout of the TARGET configuration into an empty folder (the working copy to merge into)."""
        self._guard_write(rep.target_env_id)
        self.launcher.checkout(rep.target_root_url, folder)
        self._log("checkout", url=rep.target_root_url, folder=str(folder))

    def open_merge(self, working_copy: str | Path, env_id: str) -> None:
        self._guard_write(env_id)
        self.launcher.merge(working_copy)
        self._log("merge", folder=str(working_copy))

    def _guard_write(self, env_id: str) -> None:
        self._need(Action.SVN_WRITE_TOOLS)
        env = self.connections.environments.get(env_id)
        if env.kind == EnvironmentKind.PRODUCTION:
            self._need(Action.MANAGE_ENVIRONMENTS)          # production configuration: Admin only


def _norm(text: str) -> str:
    """Line endings and a trailing newline are not a difference worth reporting."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return ""
