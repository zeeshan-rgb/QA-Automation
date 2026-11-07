"""Connect & Test: enter the Polarion URL and credentials, pick a project, run the tests."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
                               QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from ...app import AppContext
from ...domain.enums import EnvironmentKind
from ...domain.errors import PavsError
from ...domain.models import ServerProfile, split_polarion_url
from ...domain.testing import TestRunRecord
from ..theme import STATUS_COLORS
from ..widgets import heading, item, make_table, pretty
from ..workers import run_in_background

STATUS_STYLE = {"pass": "supported", "fail": "unsupported", "partial_pass": "partial", "blocked": "needs_permission",
                "not_testable": "unknown", "error": "unsupported"}
STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "partial_pass": "PARTIAL", "blocked": "BLOCKED",
               "not_testable": "NOT TESTABLE", "error": "ERROR"}
ORDER = {"fail": 0, "error": 1, "partial_pass": 2, "blocked": 3, "not_testable": 4, "pass": 5}
EXAMPLE = """REQ-1: Create a new Polarion role called Internal Reviewer. The role should have permission to review Work Items but should not have permission to modify or delete them.
Acceptance criteria:
- The role Internal Reviewer exists in the project
- Internal Reviewer can comment on Work Items
- Internal Reviewer cannot modify Work Items

REQ-2: The field Reviewer is mandatory for Requirement work items.

REQ-3: Requirement work items can move from Draft to Reviewed.
"""
FILE_FILTER = "Requirements (*.txt *.md *.csv *.xlsx *.docx *.pdf *.json *.yaml *.yml);;All files (*)"


class PasteDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paste requirements")
        self.resize(760, 480)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("One requirement per paragraph, or 'REQ-1: ...' lines. "
                                     "Acceptance criteria can follow as 'Acceptance criteria:' + bullets.")
        self.name = QLineEdit("Pasted requirements")
        ex = QPushButton("Insert example")
        ex.clicked.connect(lambda: self.text.setPlainText(EXAMPLE))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name"))
        top.addWidget(self.name, 1)
        top.addWidget(ex)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.text, 1)
        lay.addWidget(buttons)


def _status_item(status: str) -> QTableWidgetItem:
    return item(STATUS_TEXT.get(status, status), STATUS_STYLE.get(status))


class ConnectTestPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.env = None
        self.profile: ServerProfile | None = None
        self.set_id: str | None = None
        self.run_record: TestRunRecord | None = None
        self._busy = False
        self._rows: list = []
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        # ---- 1. connection
        self.saved = QComboBox()
        self._shrinkable(self.saved)
        self.saved.currentIndexChanged.connect(self._saved_changed)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://polarion-dev.example.local/polarion")
        self.url.setMinimumWidth(120)
        self.user = QLineEdit()
        self.user.setPlaceholderText("Polarion user name")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("optional: Personal Access Token")
        self.svn_user = QLineEdit()
        self.svn_user.setPlaceholderText("optional: only if /repo uses a different login")
        self.svn_password = QLineEdit()
        self.svn_password.setEchoMode(QLineEdit.Password)
        self.svn_password.setPlaceholderText("optional: repository password")
        self.local_repo = QLineEdit()
        self.local_repo.setPlaceholderText("optional: Polarion on this computer, its repository folder")
        self.local_repo.setToolTip("Only for a Polarion server running on THIS computer. The configuration files are read straight "
                                   "from its repository folder (read-only, no repository login). Leave empty to detect it automatically "
                                   "when the server is localhost and /repo refuses the login.")
        self.kind = QComboBox()
        for k in EnvironmentKind:
            self.kind.addItem(k.value.capitalize(), k)
        self.kind.setToolTip("Production environments need the environment name typed before any test data is created.")
        self.read_files = QCheckBox("Read configuration files too (needs password)")
        self.read_files.setChecked(True)
        self.read_files.setToolTip("Also reads workflow, permission and field files from the Polarion repository, read-only. Gives the most complete results.")
        self.remember = QCheckBox("Remember credentials")
        self.remember.setToolTip("Stores them in Windows Credential Manager. Leave unticked to keep them for this session only.")
        self.verify = QCheckBox("Verify TLS certificate")
        self.verify.setChecked(True)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self._connect)
        self.password.returnPressed.connect(self._connect)
        form = QFormLayout()
        form.addRow("Saved connection", self.saved)
        form.addRow("Polarion URL", self.url)
        form.addRow("Environment type", self.kind)
        form.addRow("User name", self.user)
        form.addRow("Password", self.password)
        form.addRow("Access token", self.token)
        form.addRow("Repository user", self.svn_user)
        form.addRow("Repository password", self.svn_password)
        form.addRow("Local repository folder", self.local_repo)
        opts = QVBoxLayout()
        for c in (self.read_files, self.remember, self.verify):
            opts.addWidget(c)
        form.addRow("", opts)
        self.conn_status = QLabel("Not connected.")
        self.conn_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        conn = QFrame()
        conn.setObjectName("card")
        cl = QVBoxLayout(conn)
        t1 = QLabel("1  Connect to Polarion")
        t1.setObjectName("h1")
        cl.addWidget(t1)
        cl.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(self.conn_status, 1)
        row.addWidget(self.btn_connect)
        cl.addLayout(row)
        cl.addWidget(self.progress)

        # ---- 2/3. project and tests
        self.project = QComboBox()
        self._shrinkable(self.project)
        self.project.setEditable(True)
        self.providers = QLabel("")
        self.providers.setObjectName("muted")
        self.providers.setWordWrap(True)
        self.chk_health = QCheckBox("Built-in health checks")
        self.chk_health.setChecked(True)
        self.chk_health.setToolTip("14 consistency checks: workflows, fields, enumerations, roles, permissions, documents.")
        self.chk_data = QCheckBox("Work Item data checks")
        self.chk_data.setChecked(True)
        self.chk_data.setToolTip("Read-only: reads up to 5000 Work Items and checks statuses, broken links and unlinked items.")
        self.chk_behav = QCheckBox("Behavioural tests")
        self.chk_behav.setToolTip("Really tries things: creates TEMPORARY Work Items in an approved test project, runs workflow actions, "
                                  "tries permissions as test accounts, then deletes them. You are asked to approve first.")
        self.btn_accounts = QPushButton("Test accounts...")
        self.btn_accounts.clicked.connect(self._accounts)
        self.btn_cleanup = QPushButton("Clean up test data")
        self.btn_cleanup.clicked.connect(self._cleanup)
        self.behav_state = QLabel("")
        self.behav_state.setObjectName("muted")
        self.behav_state.setWordWrap(True)
        self.chk_scripts = QCheckBox("Script analysis")
        self.chk_scripts.setToolTip("Read-only static analysis of Polarion scripts and Velocity templates (from the repository and an optional folder): "
                                    "syntax, API methods missing in this release, known bug patterns. Nothing is executed.")
        self.chk_browser = QCheckBox("Browser checks")
        self.chk_browser.setToolTip("Opens the Polarion web UI in your installed Edge or Chrome (headless, throw-away profile): login page, sign-in "
                                    "and the project page, plus JavaScript errors. Needs the Playwright package and the password. Nothing is clicked "
                                    "except the sign-in.")
        self.script_folder: str | None = None
        self.btn_script_folder = QPushButton("Folder...")
        self.btn_script_folder.setToolTip("Also analyse the scripts (.js) and templates (.vm) in a local folder.")
        self.btn_script_folder.clicked.connect(self._pick_script_folder)
        self.btn_local_scripts = QPushButton("Analyze files")
        self.btn_local_scripts.setToolTip("Analyze a folder of .js / .vm files right now, without any Polarion connection.")
        self.btn_local_scripts.clicked.connect(self._analyze_local)
        self.chk_reqs = QCheckBox("Requirement tests")
        self.chk_reqs.setChecked(True)
        self.sets = QComboBox()
        self._shrinkable(self.sets)
        self.sets.currentIndexChanged.connect(self._set_changed)
        self.btn_file = QPushButton("Load file...")
        self.btn_paste = QPushButton("Paste text...")
        self.btn_file.clicked.connect(self._load_file)
        self.btn_paste.clicked.connect(self._paste)
        self.btn_run = QPushButton("Run tests")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run)
        proj = QFrame()
        proj.setObjectName("card")
        pl = QGridLayout(proj)
        t2 = QLabel("2  Choose the project and what to test")
        t2.setObjectName("h1")
        pl.addWidget(t2, 0, 0, 1, 4)
        pl.addWidget(QLabel("Project"), 1, 0)
        pl.addWidget(self.project, 1, 1, 1, 3)
        pl.addWidget(self.providers, 2, 0, 1, 4)
        pl.addWidget(self.chk_health, 3, 0, 1, 2)
        pl.addWidget(self.chk_data, 3, 2, 1, 2)
        pl.addWidget(self.chk_reqs, 4, 0)
        pl.addWidget(self.sets, 4, 1)
        pl.addWidget(self.btn_file, 4, 2)
        pl.addWidget(self.btn_paste, 4, 3)
        pl.addWidget(self.chk_behav, 5, 0)
        pl.addWidget(self.btn_accounts, 5, 2)
        pl.addWidget(self.btn_cleanup, 5, 3)
        pl.addWidget(self.chk_scripts, 6, 0)
        pl.addWidget(self.btn_script_folder, 6, 2)
        pl.addWidget(self.btn_local_scripts, 6, 3)
        pl.addWidget(self.chk_browser, 7, 0)
        pl.addWidget(self.behav_state, 8, 0, 1, 4)
        pl.addWidget(self.btn_run, 9, 3)
        pl.setColumnStretch(1, 1)
        self.project_card = proj

        # ---- results
        self.verdict = QLabel("No tests run yet.")
        self.verdict.setObjectName("h1")
        self.verdict.setMinimumWidth(260)
        self.counts = QLabel("")
        self.counts.setObjectName("muted")
        self.filter = QComboBox()
        self._shrinkable(self.filter)
        self.filter.addItem("All results", None)
        for k, v in STATUS_TEXT.items():
            self.filter.addItem(v.title(), k)
        self.filter.currentIndexChanged.connect(self._fill_results)
        self.btn_export = QPushButton("Export report...")
        self.btn_export.clicked.connect(self._export)
        self.btn_export.setEnabled(False)
        self.table = make_table(["Status", "Group", "Check", "Requirement", "Expected", "Actual"])
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        res = QWidget()
        rl = QVBoxLayout(res)
        head = QHBoxLayout()
        head.addWidget(self.verdict)
        head.addStretch(1)
        head.addWidget(self.filter)
        head.addWidget(self.btn_export)
        rl.addLayout(head)
        rl.addWidget(self.counts)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([320, 160])
        rl.addWidget(split, 1)

        # ---- requirements tab
        self.req_table = make_table(["ID", "Requirement", "Criteria", "Source"])
        self.case_table = make_table(["Run", "Requirement", "Test", "Rule", "Kind"], stretch_last=True)
        self.case_table.itemChanged.connect(self._case_toggled)
        self.findings = QTextEdit()
        self.findings.setReadOnly(True)
        rq = QSplitter(Qt.Vertical)
        rq.addWidget(self.req_table)
        rq.addWidget(self.case_table)
        rq.addWidget(self.findings)
        rq.setSizes([150, 220, 120])

        self.gaps = QTextEdit()
        self.gaps.setReadOnly(True)
        self.history = make_table(["When", "Project", "Type", "Verdict", "Checks", "Pass", "Fail"])
        self.history.itemSelectionChanged.connect(self._history_selected)
        self.btn_compare = QPushButton("Compare with previous run")
        self.btn_compare.clicked.connect(self._compare_runs)
        self.btn_delete_run = QPushButton("Delete selected")
        self.btn_delete_run.clicked.connect(self._delete_selected_runs)
        self.btn_clear = QPushButton("Clear history...")
        self.btn_clear.setToolTip("Delete saved test results. Exported reports, saved connections and test-data cleanup records are not touched.")
        self.btn_clear.clicked.connect(self._clear_history)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(420)     # the page scrolls on short screens instead of squeezing the results
        self.tabs.addTab(res, "Results")
        self.tabs.addTab(rq, "Requirements && generated tests")
        self.tabs.addTab(self.gaps, "Could not be inspected")
        hist = QWidget()
        hl = QVBoxLayout(hist)
        bar = QHBoxLayout()
        bar.addWidget(self.btn_compare)
        bar.addWidget(self.btn_delete_run)
        bar.addStretch(1)
        bar.addWidget(self.btn_clear)
        hl.addLayout(bar)
        hl.addWidget(self.history, 1)
        self.tabs.addTab(hist, "History")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(heading("Connect & Test",
                              "Enter your Polarion address and credentials, pick a project, and run the tests. Read-only by default. "
                              "Only the optional Behavioural tests create temporary test data, and only in a test project you approve."))
        top = QHBoxLayout()
        top.addWidget(conn, 1)
        top.addWidget(proj, 1)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        self._set_project_enabled(False)

    @staticmethod
    def _shrinkable(box: QComboBox) -> None:
        box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        box.setMinimumContentsLength(8)

    # ------------------------------------------------------------------ helpers
    def refresh(self) -> None:
        cur = self.saved.currentData()
        self.saved.blockSignals(True)
        self.saved.clear()
        self.saved.addItem("New connection", None)
        for e in self.ctx.environments.list():
            self.saved.addItem(f"{e.name}", e.id)
        idx = self.saved.findData(cur)
        self.saved.setCurrentIndex(idx if idx >= 0 else 0)
        self.saved.blockSignals(False)
        self._load_sets()
        self._fill_history()

    def _set_project_enabled(self, on: bool) -> None:
        self.btn_local_scripts.setEnabled(not self._busy)
        for w in (self.project, self.chk_health, self.chk_data, self.chk_scripts, self.chk_browser, self.btn_script_folder, self.chk_reqs, self.chk_behav, self.btn_accounts, self.btn_cleanup,
                  self.sets, self.btn_file, self.btn_paste, self.btn_run):
            w.setEnabled(on and not self._busy)

    def _busy_state(self, busy: bool) -> None:
        self._busy = busy
        self.btn_connect.setEnabled(not busy)
        self.progress.setVisible(busy)
        self._set_project_enabled(self.profile is not None and self.env is not None and self._connected_ok)

    _connected_ok = False

    def _saved_changed(self) -> None:
        env_id = self.saved.currentData()
        if not env_id:
            return
        env = self.ctx.environments.get(env_id)
        self.url.setText(env.base_url)
        self.user.setText(env.username or "")
        self.local_repo.setText(env.local_repo_path or "")
        self.verify.setChecked(env.tls.verify)
        self.kind.setCurrentIndex(max(0, self.kind.findData(env.kind)))
        stored = self.ctx.environments.has_credentials(env_id)
        self.password.setPlaceholderText("stored" if stored["password"] else "")
        self.token.setPlaceholderText("stored" if stored["token"] else "optional: Personal Access Token")
        self.svn_password.setPlaceholderText("stored" if stored.get("svn_password") else "optional: repository password")

    def _progress(self, msg: str, pct: int) -> None:
        self.conn_status.setText(msg)
        self.progress.setValue(pct)

    # ------------------------------------------------------------------ connect
    def _connect(self) -> None:
        if self._busy:
            return
        url, user, pw, tok = self.url.text().strip(), self.user.text().strip(), self.password.text(), self.token.text().strip()
        env_id = self.saved.currentData()
        svn_user, svn_pw = self.svn_user.text().strip(), self.svn_password.text()
        local_repo = self.local_repo.text().strip()
        if env_id:                                  # anything left blank is taken from the saved connection
            stored = self.ctx.environments.credentials(env_id)
            if stored:
                pw, tok = pw or stored.password or "", tok or stored.token or ""
                svn_user, svn_pw = svn_user or stored.svn_user or "", svn_pw or stored.svn_password or ""
        if not url:
            self.conn_status.setText("Enter the Polarion URL.")
            return
        try:
            base, hint = split_polarion_url(url)
        except ValueError as exc:
            self.conn_status.setStyleSheet("color: #b42318;")
            self.conn_status.setText(str(exc))
            return
        if base != url.rstrip("/"):
            self.url.setText(base)            # a pasted page address is reduced to the server address
            url = base
        self._connected_ok = False
        self._busy_state(True)
        self.progress.setValue(0)
        self.conn_status.setStyleSheet("")
        self.conn_status.setText("Connecting ...")
        read_files, remember, verify = self.read_files.isChecked(), self.remember.isChecked(), self.verify.isChecked()
        kind = EnvironmentKind(self.kind.currentData() or "development")

        def work(progress):
            return self.ctx.connections.quick_connect(url, user, pw, tok, read_files, remember, verify, progress=progress, kind=kind,
                                                 svn_user=svn_user, svn_password=svn_pw, local_repo=local_repo)

        def done(out) -> None:
            self.env, self.profile = out.environment, out.profile
            self._connected_ok = out.ok
            self.conn_status.setStyleSheet(f"color: {'#1e7a3c' if out.ok else '#b42318'}; font-weight: 600;")
            self.conn_status.setText(out.message)
            self.password.clear()
            self.token.clear()
            self.svn_password.clear()
            if out.ok:
                self._fill_projects()
                if hint:
                    i = self.project.findData(hint)
                    if i < 0:
                        i = self.project.findText(hint, Qt.MatchStartsWith)
                    if i >= 0:
                        self.project.setCurrentIndex(i)
                        self.conn_status.setText(out.message + f" Project {hint} selected from the address.")
            self.refresh_saved_keep(out.environment.id if out.environment else None)

        run_in_background(work, done, lambda msg: (self.conn_status.setStyleSheet("color:#b42318"),
                                                   self.conn_status.setText(msg)),
                          self._progress, lambda: self._busy_state(False))

    def refresh_saved_keep(self, env_id: str | None) -> None:
        self.refresh()
        if env_id:
            idx = self.saved.findData(env_id)
            if idx >= 0:
                self.saved.blockSignals(True)
                self.saved.setCurrentIndex(idx)
                self.saved.blockSignals(False)

    def _fill_projects(self) -> None:
        p = self.profile
        self.project.clear()
        for pr in p.projects:
            self.project.addItem(f"{pr.id}  -  {pr.name}" if pr.name and pr.name != pr.id else pr.id, pr.id)
        prov = ", ".join(f"{x.kind.value.upper()}: {pretty(x.state.value)}" for x in p.providers)
        extra = "" if p.projects else "  No projects were listed (the account may lack access); type a project id."
        warn = "  " + " ".join(w for w in p.warnings if "validated" not in w)[:300] if p.warnings else ""
        self.providers.setText(f"Polarion {p.version.release or '?'} - {prov}.{extra}{warn}")
        self._set_project_enabled(True)

    def _project_id(self) -> str:
        data = self.project.currentData()
        text = self.project.currentText().strip()
        return data if data and text.startswith(str(data)) else text.split("  -  ")[0].strip()

    # ------------------------------------------------------------------ requirements
    def _load_sets(self) -> None:
        cur = self.set_id
        self.sets.blockSignals(True)
        self.sets.clear()
        self.sets.addItem("(no requirements loaded)", None)
        for rs in self.ctx.testing.sets():
            self.sets.addItem(f"{rs.name}  ({rs.imported_at.astimezone():%d %b %H:%M})", rs.id)
        idx = self.sets.findData(cur)
        self.sets.setCurrentIndex(idx if idx >= 0 else 0)
        self.sets.blockSignals(False)

    def _set_changed(self) -> None:
        self.set_id = self.sets.currentData()
        self._show_analysis()

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load requirements", "", FILE_FILTER)
        if path:
            self._import(lambda: self.ctx.testing.import_path(path, self._env_id(), self._project_id() or None))

    def _paste(self) -> None:
        dlg = PasteDialog(self)
        if dlg.exec() and dlg.text.toPlainText().strip():
            text, name = dlg.text.toPlainText(), dlg.name.text().strip() or "Pasted requirements"
            self._import(lambda: self.ctx.testing.import_pasted(text, name, self._env_id(), self._project_id() or None))

    def _env_id(self) -> str | None:
        return self.env.id if self.env else None

    def _import(self, fn) -> None:
        try:
            a = fn()
        except Exception as exc:
            QMessageBox.warning(self, "Requirements", str(exc))
            return
        self.set_id = a.requirement_set.id
        self._load_sets()
        self._show_analysis(a)
        self.tabs.setCurrentIndex(1)
        if a.warnings:
            QMessageBox.information(self, "Requirements loaded", "\n".join(a.warnings))

    def _show_analysis(self, analysis=None) -> None:
        self.req_table.setRowCount(0)
        self.case_table.blockSignals(True)
        self.case_table.setRowCount(0)
        self.findings.clear()
        if not self.set_id:
            self.case_table.blockSignals(False)
            return
        a = analysis or self.ctx.testing.load(self.set_id)
        self.req_table.setRowCount(len(a.requirements))
        labels = {r.id: r.label for r in a.requirements}
        for i, r in enumerate(a.requirements):
            for c, v in enumerate([r.external_id, r.text, str(len(r.acceptance_criteria)), r.source]):
                self.req_table.setItem(i, c, item(v, tooltip=v if len(v) > 60 else None))
        self.case_table.setRowCount(len(a.cases))
        for i, tc in enumerate(a.cases):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked if tc.status.value == "rejected" else Qt.Checked)
            chk.setData(Qt.UserRole, tc.id)
            self.case_table.setItem(i, 0, chk)
            for c, v in enumerate([labels.get(tc.requirement_id, ""), tc.title, tc.rationale, tc.origin.value.replace("_", " ")], 1):
                self.case_table.setItem(i, c, item(v, tooltip=tc.check.describe()))
        self.case_table.blockSignals(False)
        lines = []
        for f in a.findings:
            lines.append(f"<li><b>{pretty(f.kind.value)}</b> ({labels.get(f.requirement_id, f.requirement_id)}): {f.message}</li>")
        self.findings.setHtml(f"<p><b>{len(a.cases)} test(s) generated by fixed rules.</b> "
                              f"Untick a test to skip it. Findings:</p><ul>{''.join(lines) or '<li>none</li>'}</ul>")

    def _case_toggled(self, it: QTableWidgetItem) -> None:
        if it.column() == 0 and self.set_id:
            self.ctx.testing.set_case_enabled(it.data(Qt.UserRole), self.set_id, it.checkState() == Qt.Checked)

    # ------------------------------------------------------------------ run
    def _run(self) -> None:
        if self._busy or self.env is None:
            return
        project = self._project_id()
        if not project:
            QMessageBox.information(self, "Run tests", "Choose a project first.")
            return
        health, data = self.chk_health.isChecked(), self.chk_data.isChecked()
        scripts, folder = self.chk_scripts.isChecked(), self.script_folder
        set_id = self.set_id if self.chk_reqs.isChecked() else None
        browser = self.chk_browser.isChecked()
        if not health and not data and not set_id and not scripts and not browser:
            QMessageBox.information(self, "Run tests", "Tick at least one kind of test, or load requirements.")
            return
        behavioural, confirmation = self.chk_behav.isChecked(), None
        if behavioural:
            confirmation = self._confirm_behavioural(project)
            if confirmation is False:
                return
        self._busy_state(True)
        self.verdict.setText("Running tests ...")
        self.counts.setText("")
        self.tabs.setCurrentIndex(0)
        env_id = self.env.id

        def done(rec: TestRunRecord) -> None:
            self.run_record = rec
            self._show_run(rec)
            self._fill_history()
            self._update_behav_state()
            self._show_regression(rec)

        run_in_background(lambda progress: self.ctx.testing.run(env_id, project, health=health, set_id=set_id, progress=progress,
                                                                data=data, behavioural=behavioural, scripts=scripts, script_folder=folder, browser=browser,
                                                                production_confirmation=confirmation or None),
                          done, lambda msg: (self.verdict.setText("The test run failed."), self.counts.setText(msg)),
                          lambda msg, pct: (self.counts.setText(msg), self.progress.setValue(pct)),
                          lambda: self._busy_state(False))

    # ------------------------------------------------------------------ behavioural tests: approval, accounts, cleanup
    def _env(self):
        return self.ctx.environments.get(self.env.id) if self.env else None

    def _update_behav_state(self) -> None:
        env = self._env()
        if env is None:
            self.behav_state.setText("")
            return
        left = self.ctx.testing.leftover_artifacts(env.id)
        allowed = ", ".join(env.test_projects) or "none yet"
        accounts = ", ".join(f"{r}={u}" for r, u in env.test_accounts.items()) or "none"
        text = f"Test data may be created only in: {allowed}.  Test accounts: {accounts}."
        if left:
            text += f"  {len(left)} test Work Item(s) from an earlier run are still recorded: use Clean up test data."
        self.behav_state.setText(text)
        self.behav_state.setStyleSheet("color:#b42318" if left else "")

    def _confirm_behavioural(self, project: str):
        """Ask the user for the explicit approval. Returns the production confirmation text ('' if none) or False to cancel."""
        env = self._env()
        if env is None:
            return False
        if project not in env.test_projects:
            accounts = ", ".join(f"{r} as {u}" for r, u in env.test_accounts.items()) or "no test accounts (permission tests will not run as a real user)"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Allow test data in this project?")
            box.setText(f"Allow this tool to create TEMPORARY test data in project '{project}' on {env.base_url}?")
            box.setInformativeText(
                "This is the only part of the application that changes anything in Polarion.\n\n"
                f"- It will create up to 30 Work Items titled [PAVS-...] in project '{project}', change them (workflow actions, "
                "edits, comments) and delete them again when the run ends.\n"
                "- Nothing outside this project is ever touched, and only items it created itself are ever deleted.\n"
                f"- Test accounts: {accounts}.\n"
                "- Use a dedicated TEST project, never your real working project.\n"
                "- The approval is saved for this project; you can withdraw it any time under Test accounts...")
            allow = box.addButton("Allow and run", QMessageBox.AcceptRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not allow:
                return False
            import getpass
            self.ctx.environments.allow_writes_in(env.id, project, getpass.getuser())
            env = self._env()
        confirmation = ""
        if env.kind == EnvironmentKind.PRODUCTION:
            typed, ok = QInputDialog.getText(self, "Production environment",
                                             f"'{env.name}' is marked as PRODUCTION.\nType its exact name to allow test data: {env.name}")
            if not ok:
                return False
            confirmation = typed.strip()
        self._update_behav_state()
        return confirmation

    def _accounts(self) -> None:
        env = self._env()
        if env is None:
            return
        dlg = TestAccountsDialog(self.ctx, env, self._project_id(), self)
        dlg.exec()
        self._update_behav_state()

    def _cleanup(self) -> None:
        env = self._env()
        project = self._project_id()
        if env is None or not project or self._busy:
            return
        left = self.ctx.testing.leftover_artifacts(env.id, project)
        if not left:
            QMessageBox.information(self, "Clean up test data", f"No test Work Items are recorded for project '{project}'.")
            return
        if QMessageBox.question(self, "Clean up test data",
                                f"Delete {len(left)} leftover test Work Item(s) ({', '.join(a.ref for a in left[:8])}) from project '{project}'?\n"
                                "Only items this tool created and tagged are deleted.") != QMessageBox.Yes:
            return
        confirmation = None
        if env.kind == EnvironmentKind.PRODUCTION:
            typed, ok = QInputDialog.getText(self, "Production environment", f"Type the environment name to confirm: {env.name}")
            if not ok:
                return
            confirmation = typed.strip()
        self._busy_state(True)

        def done(result) -> None:
            deleted, leftovers, log = result
            QMessageBox.information(self, "Clean up test data", f"Deleted {deleted}. Left in place: {', '.join(leftovers) or 'none'}.\n" + "\n".join(log))

        run_in_background(lambda p: self.ctx.testing.cleanup(env.id, project, confirmation), done,
                          lambda msg: QMessageBox.warning(self, "Clean up failed", msg), None,
                          lambda: (self._busy_state(False), self._update_behav_state()))

    # ------------------------------------------------------------------ scripts and regression
    def _pick_script_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Folder with scripts (.js) and Velocity templates (.vm)", self.script_folder or "")
        if path:
            self.script_folder = path
            self.btn_script_folder.setText(path.replace("\\", "/").rsplit("/", 1)[-1][:12])
            self.btn_script_folder.setToolTip(path)
            self.chk_scripts.setChecked(True)

    def _analyze_local(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Analyze this folder of scripts and templates", self.script_folder or "")
        if not path or self._busy:
            return
        release = self.profile.version.release if self.profile else None
        self.verdict.setText("Analysing ...")
        self.counts.setText(path)
        self.tabs.setCurrentIndex(0)
        self._busy_state(True)

        def done(rec: TestRunRecord) -> None:
            self.run_record = rec
            self._show_run(rec)
            self._fill_history()

        run_in_background(lambda p: self.ctx.testing.analyze_local_scripts(path, release), done,
                          lambda msg: (self.verdict.setText("Analysis failed."), self.counts.setText(msg)), None,
                          lambda: self._busy_state(False))

    def _show_regression(self, rec: TestRunRecord) -> None:
        from ...services.regression import diff_runs, summarize
        prev = self.ctx.testing.previous_run(rec)
        if prev is None:
            return
        s = summarize(diff_runs(prev, rec))
        bad = s.get("regression", 0) + s.get("new_problem", 0)
        self.counts.setText(self.counts.text() + f"    |    since the previous run: {bad} worse, {s.get('fixed', 0)} fixed, {s.get('changed', 0)} changed")
        if bad:
            self.counts.setStyleSheet("color:#b42318")

    def _compare_runs(self) -> None:
        rows = self.history.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "Compare", "Select a run in the list first.")
            return
        new = self._hist[rows[0].row()]
        old = next((r for r in self._hist[rows[0].row() + 1:] if r.project_id == new.project_id and r.kind == new.kind), None)
        if old is None:
            QMessageBox.information(self, "Compare", "There is no earlier run of the same project and type to compare with.")
            return
        changes = self.ctx.testing.compare_runs(old.id, new.id)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{new.project_id}: {old.started_at.astimezone():%d %b %H:%M} -> {new.started_at.astimezone():%d %b %H:%M}")
        dlg.resize(980, 520)
        t = make_table(["Change", "Group", "Check", "Requirement", "Before", "Now", "Now: actual"])
        shown = [c for c in changes if c.kind != "same"]
        t.setRowCount(len(shown))
        style = {"regression": "unsupported", "new_problem": "unsupported", "fixed": "supported", "changed": "partial", "new": "needs_permission", "removed": "unknown"}
        for i, c in enumerate(shown):
            for col, v in enumerate([c.kind.replace("_", " ").upper(), c.group, c.name, c.requirement, c.old_status, c.new_status, c.new_actual]):
                t.setItem(i, col, item(v, style.get(c.kind) if col == 0 else None, tooltip=v if len(v) > 50 else None))
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"{len(shown)} difference(s); {len(changes) - len(shown)} check(s) unchanged. Worst first."))
        lay.addWidget(t, 1)
        dlg.exec()

    def _show_run(self, rec: TestRunRecord) -> None:
        c = rec.counts()
        color = {"PASSED": "#1e7a3c", "FAILED": "#b42318", "PASSED WITH GAPS": "#946200"}.get(rec.verdict, "#1d2433")
        self.verdict.setText(f"{rec.verdict}")
        self.verdict.setStyleSheet(f"color: {color};")
        self.counts.setText(f"{rec.total} checks on {rec.project_id} (Polarion {rec.release or '?'}):  "
                            f"{c['pass']} pass · {c['fail']} fail · {c['partial_pass']} partial · {c['blocked']} blocked · "
                            f"{c['not_testable']} not testable · {c['error']} error    |    coverage {rec.coverage_pct}%")
        self.btn_export.setEnabled(True)
        self.gaps.setHtml("<p>What could not be read, and how to fix it:</p><ul>"
                          + ("".join(f"<li>{g}</li>" for g in rec.inspection_gaps) or "<li>Nothing - every section was read.</li>")
                          + "</ul>")
        self._fill_results()

    def _fill_results(self) -> None:
        rec = self.run_record
        self.table.setRowCount(0)
        self.detail.clear()
        if rec is None:
            return
        want = self.filter.currentData()
        rows = sorted((r for r in rec.results if want is None or r.status.value == want),
                      key=lambda r: (ORDER[r.status.value], r.group, r.check_id))
        self._rows = rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, _status_item(r.status.value))
            for c, v in enumerate([r.group, r.check_id if r.group == "Health checks" else "", r.requirement_label,
                                   r.expected, r.actual], 1):
                self.table.setItem(i, c, item(v, tooltip=v if len(v) > 50 else None))
            self.table.item(i, 2).setText(f"{r.check_id}  {r.name}" if r.group == "Health checks" else r.name)

    def _show_detail(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._rows):
            return
        r = self._rows[rows[0].row()]
        ev = "".join(f"<li>{_esc(x)}</li>" for x in r.evidence) or "<li>-</li>"
        self.detail.setHtml(
            f"<h3>{_esc(r.name)} - {STATUS_TEXT[r.status.value]}</h3>"
            + (f"<p><b>Requirement:</b> {_esc(r.requirement_label)}" + (f" - {_esc(r.criterion)}" if r.criterion else "") + "</p>" if r.requirement_label else "")
            + f"<p><b>Expected:</b> {_esc(r.expected)}<br><b>Actual:</b> {_esc(r.actual)}<br><b>How it was checked:</b> {_esc(r.mechanism)}</p>"
            f"<p><b>Evidence</b></p><ul>{ev}</ul>")

    def _export(self) -> None:
        if self.run_record is None:
            return
        default = f"test-report-{self.run_record.project_id}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export report", default,
                                              "Excel (*.xlsx);;PDF (*.pdf);;HTML (*.html);;CSV (*.csv)")
        if not path:
            return
        try:
            out = self.ctx.testing.export(self.run_record.id, path)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Report saved", f"Saved to {out}")

    # ------------------------------------------------------------------ history
    def _fill_history(self) -> None:
        runs = self.ctx.testing.history(self._env_id()) if self.env else self.ctx.testing.history()
        self._hist = runs
        self.history.setRowCount(len(runs))
        for i, r in enumerate(runs):
            c = r.counts()
            vals = [f"{r.started_at.astimezone():%Y-%m-%d %H:%M}", r.project_id, r.kind, r.verdict, str(r.total),
                    str(c["pass"]), str(c["fail"])]
            for col, v in enumerate(vals):
                key = None
                if col == 3:
                    key = {"PASSED": "supported", "FAILED": "unsupported", "PASSED WITH GAPS": "partial"}.get(v)
                self.history.setItem(i, col, item(v, key))

    def _delete_selected_runs(self) -> None:
        rows = sorted({i.row() for i in self.history.selectionModel().selectedRows()})
        ids = [self._hist[r].id for r in rows if r < len(self._hist)]
        if not ids:
            QMessageBox.information(self, "History", "Select a run in the list first.")
            return
        if QMessageBox.question(self, "Delete selected", f"Delete {len(ids)} saved test run(s)? This cannot be undone.") != QMessageBox.Yes:
            return
        self._history_action(lambda: self.ctx.testing.delete_runs(ids))

    def _clear_history(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Clear history")
        box.setText("Delete saved test results?")
        box.setInformativeText("Exported reports, saved connections and the records used to clean up test data are not touched. "
                               "This cannot be undone.")
        mine = box.addButton("This connection only" if self.env else "Everything", QMessageBox.AcceptRole)
        everything = box.addButton("Everything", QMessageBox.DestructiveRole) if self.env else None
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is mine:
            self._history_action(lambda: self.ctx.testing.clear_history(self.env.id if self.env else None))
        elif everything is not None and clicked is everything:
            self._history_action(lambda: self.ctx.testing.clear_history())

    def _history_action(self, fn) -> None:
        try:
            n = fn()
        except PavsError as exc:
            QMessageBox.warning(self, "History", str(exc))
            return
        self.run_record = None
        self._fill_history()
        self.counts.setText(f"{n} saved run(s) deleted.")

    def _history_selected(self) -> None:
        rows = self.history.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._hist):
            self.run_record = self._hist[rows[0].row()]
            self._show_run(self.run_record)
            self.tabs.setCurrentIndex(0)


class TestAccountsDialog(QDialog):
    """Test accounts (one Polarion user per role, with a Personal Access Token) and approved test projects."""

    def __init__(self, ctx: AppContext, env, project: str, parent=None) -> None:
        super().__init__(parent)
        self.ctx, self.env_id, self.project = ctx, env.id, project
        self.setWindowTitle("Test accounts and test projects")
        self.resize(640, 460)
        self.table = make_table(["Role", "User", "Token"])
        self.role = QLineEdit()
        self.role.setPlaceholderText("role id, e.g. internal_reviewer")
        self.user = QLineEdit()
        self.user.setPlaceholderText("Polarion user of the test account")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("its Personal Access Token")
        self.keep = QCheckBox("Remember the token (Windows Credential Manager)")
        add = QPushButton("Add / update")
        add.clicked.connect(self._add)
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self._remove)
        self.projects = QLabel("")
        self.projects.setWordWrap(True)
        revoke = QPushButton("Withdraw approval for this project")
        revoke.clicked.connect(self._revoke)
        note = QLabel("A test account is a real Polarion user who holds the role under test. The tool signs in as that user "
                      "(REST, token) and really tries the action in the test project, to confirm what the role may or may not do. "
                      "The account needs REST endpoint permissions (GET/POST/PATCH/DELETE) or the result is reported as BLOCKED, not guessed.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form = QHBoxLayout()
        for w in (self.role, self.user, self.token):
            form.addWidget(w, 1)
        form.addWidget(add)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(note)
        lay.addWidget(self.table, 1)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(self.keep, 1)
        row.addWidget(rm)
        lay.addLayout(row)
        lay.addWidget(self.projects)
        lay.addWidget(revoke)
        lay.addWidget(buttons)
        self._fill()

    def _fill(self) -> None:
        env = self.ctx.environments.get(self.env_id)
        self.table.setRowCount(len(env.test_accounts))
        for i, (role, user) in enumerate(sorted(env.test_accounts.items())):
            has = self.ctx.environments.test_account_token(self.env_id, user)
            for c, v in enumerate([role, user, "set" if has else "missing (re-enter)"]):
                self.table.setItem(i, c, item(v))
        self.projects.setText("Projects approved for test data: " + (", ".join(env.test_projects) or "none"))

    def _add(self) -> None:
        role, user, tok = self.role.text().strip(), self.user.text().strip(), self.token.text().strip()
        if not (role and user and tok):
            QMessageBox.information(self, "Test account", "Enter the role, the user and the token.")
            return
        try:
            self.ctx.environments.set_test_account(self.env_id, role, user, tok, persist=self.keep.isChecked())
        except PavsError as exc:
            QMessageBox.warning(self, "Test account", str(exc))
            return
        self.token.clear()
        self._fill()

    def _remove(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.ctx.environments.remove_test_account(self.env_id, self.table.item(rows[0].row(), 0).text())
            self._fill()

    def _revoke(self) -> None:
        if self.project:
            self.ctx.environments.revoke_writes_in(self.env_id, self.project)
            self._fill()


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
