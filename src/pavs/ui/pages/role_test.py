"""Role Test: pick a Polarion user and roles, assign them in a test project, really do the work, and read the blockers."""
from __future__ import annotations

import getpass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
                               QTabWidget, QVBoxLayout, QWidget)

from ...app import AppContext
from ...domain.enums import EnvironmentKind
from ...domain.errors import PavsError
from ..widgets import heading, item, make_table
from ..workers import run_in_background

STATUS_STYLE = {"pass": "supported", "fail": "unsupported", "partial_pass": "partial", "blocked": "needs_permission",
                "not_testable": "unknown", "error": "unsupported"}
STATUS_TEXT = {"pass": "PASS", "fail": "FAIL", "partial_pass": "PARTIAL", "blocked": "BLOCKED", "not_testable": "NOT TESTABLE", "error": "ERROR"}
ORDER = {"fail": 0, "error": 1, "blocked": 2, "partial_pass": 3, "not_testable": 4, "pass": 5}


class RoleTestPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.matrix = None
        self.outcome = None
        self._busy = False
        self._users: list = []

        self.env_box, self.project_box = QComboBox(), QComboBox()
        for b in (self.env_box, self.project_box):
            b.setMinimumContentsLength(14)
            b.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.env_box.currentIndexChanged.connect(self._env_changed)
        self.project_box.currentIndexChanged.connect(self._project_changed)
        self.btn_load = QPushButton("Load users and roles from Polarion")
        self.btn_load.clicked.connect(self._load)

        self.users = QListWidget()
        self.users.setSelectionMode(QListWidget.SingleSelection)
        self.users.currentRowChanged.connect(self._user_chosen)
        self.user_now = QLabel("")
        self.user_now.setObjectName("muted")
        self.user_now.setWordWrap(True)
        self.roles = QListWidget()
        self.types = QListWidget()
        for lst in (self.users, self.roles, self.types):
            lst.setMinimumHeight(150)
            lst.setMaximumHeight(210)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("password of the selected user")
        self.password.setToolTip("The test signs in as this user. The password is used for this run only: it is never saved or written to a log.")
        self.mode_auto = QRadioButton("Assign the chosen roles to the user for me, then restore the original roles")
        self.mode_have = QRadioButton("The user already has the role(s): I assigned them in Polarion (nothing is changed)")
        self.mode_auto.setChecked(True)
        self.restore = QCheckBox("Restore the user's original roles when the test ends")
        self.restore.setChecked(True)
        self.mode_auto.toggled.connect(self._mode_changed)
        self.cap_note = QLabel("")
        self.cap_note.setObjectName("muted")
        self.cap_note.setWordWrap(True)

        self.sheet_label = QLabel("No Excel sheet loaded: every action the test tries is expected to work.")
        self.sheet_label.setObjectName("muted")
        self.sheet_label.setWordWrap(True)
        self.btn_sheet = QPushButton("Load expected permissions (Excel)...")
        self.btn_sheet.clicked.connect(self._load_sheet)
        self.sheet_box = QComboBox()
        self.sheet_box.setVisible(False)
        self.sheet_box.currentIndexChanged.connect(self._sheet_chosen)
        self.btn_sheet_clear = QPushButton("Clear")
        self.btn_sheet_clear.clicked.connect(self._clear_sheet)
        self.btn_sheet_clear.setVisible(False)
        self._sheet_path = ""

        self.area_wi = QCheckBox("Work Items")
        self.area_doc = QCheckBox("Documents")
        self.area_other = QCheckBox("Test runs and collections (asked of the server)")
        for c in (self.area_wi, self.area_doc, self.area_other):
            c.setChecked(True)
        self.space_box = QComboBox()
        self.space_box.setToolTip("Document space used for the test documents")

        self.btn_run = QPushButton("Run role test")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run)
        self.btn_export = QPushButton("Export report...")
        self.btn_export.clicked.connect(self._export)
        self.btn_copy = QPushButton("Copy blockers")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_restore = QPushButton("Restore roles")
        self.btn_restore.clicked.connect(self._restore_pending)
        self.btn_cleanup = QPushButton("Clean up test data")
        self.btn_cleanup.clicked.connect(self._cleanup)
        for b in (self.btn_export, self.btn_copy):
            b.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("Connect on the 'Connect & Test' page, choose the environment and project, then load the users.")
        self.status.setWordWrap(True)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color:#946200;")

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.table = make_table(["Status", "Where", "Check", "Expected", "Actual", "Detail"])
        self.tabs = QTabWidget()
        self.tabs.addTab(self.report_text, "Blockers report")
        self.tabs.addTab(self.table, "All checks")

        top = QHBoxLayout()
        top.addWidget(QLabel("Environment"))
        top.addWidget(self.env_box)
        top.addWidget(QLabel("Test project"))
        top.addWidget(self.project_box)
        top.addWidget(self.btn_load)
        top.addStretch(1)
        lists = QGridLayout()
        for col, (title, w) in enumerate((("1. User", self.users), ("2. Role(s) to give", self.roles), ("3. Work Item types to test", self.types))):
            lists.addWidget(QLabel(title), 0, col)
            lists.addWidget(w, 1, col)
        lists.addWidget(self.user_now, 2, 0)
        opts = QVBoxLayout()
        pw = QHBoxLayout()
        pw.addWidget(QLabel("4. Password of the user"))
        pw.addWidget(self.password, 1)
        opts.addLayout(pw)
        opts.addWidget(self.mode_auto)
        opts.addWidget(self.mode_have)
        opts.addWidget(self.restore)
        opts.addWidget(self.cap_note)
        sheet = QHBoxLayout()
        sheet.addWidget(self.btn_sheet)
        sheet.addWidget(self.sheet_box)
        sheet.addWidget(self.btn_sheet_clear)
        sheet.addStretch(1)
        opts.addLayout(sheet)
        opts.addWidget(self.sheet_label)
        areas = QHBoxLayout()
        areas.addWidget(QLabel("Test:"))
        for c in (self.area_wi, self.area_doc, self.area_other):
            areas.addWidget(c)
        areas.addWidget(QLabel("Document space"))
        areas.addWidget(self.space_box)
        areas.addStretch(1)
        opts.addLayout(areas)
        actions = QHBoxLayout()
        for b in (self.btn_run, self.btn_export, self.btn_copy, self.btn_restore, self.btn_cleanup):
            actions.addWidget(b)
        actions.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Role Test",
                              "Choose a Polarion user and the role(s) to give them in a TEST project. The app assigns the role, signs in as that user, "
                              "really creates, changes, comments on and deletes test Work Items and documents, then removes the test data and puts the "
                              "user's roles back. If something stands in the way, the report lists the blockers and what to fix."))
        lay.addLayout(top)
        lay.addWidget(self.banner)
        lay.addLayout(lists)
        lay.addLayout(opts)
        lay.addLayout(actions)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addWidget(self.summary)
        lay.addWidget(self.tabs, 1)
        self._mode_changed()

    # ------------------------------------------------------------------ environment / project
    def refresh(self) -> None:
        current = self.env_box.currentData()
        self.env_box.blockSignals(True)
        self.env_box.clear()
        for e in self.ctx.environments.list():
            self.env_box.addItem(e.name, e.id)
        self.env_box.setCurrentIndex(max(self.env_box.findData(current), 0))
        self.env_box.blockSignals(False)
        self._env_changed()

    def _env_id(self) -> str | None:
        return self.env_box.currentData()

    def _project(self) -> str:
        return self.project_box.currentText().strip()

    def _env_changed(self) -> None:
        current = self.project_box.currentText()
        self.project_box.blockSignals(True)
        self.project_box.clear()
        env_id = self._env_id()
        prof = self.ctx.connections.latest_profile(env_id) if env_id else None
        env = self.ctx.environments.get(env_id) if env_id else None
        names = [p.id for p in (prof.projects if prof else [])]
        for n in sorted(names, key=lambda n: (env is not None and n not in env.test_projects, n.lower())):    # approved test projects first
            self.project_box.addItem(n)
        i = self.project_box.findText(current)
        if i >= 0:
            self.project_box.setCurrentIndex(i)
        self.project_box.blockSignals(False)
        self._project_changed()

    def _project_changed(self) -> None:
        self.users.clear()
        self.roles.clear()
        self.types.clear()
        self.space_box.clear()
        self.user_now.setText("")
        self._users = []
        self._update_banner()

    def _update_banner(self) -> None:
        env_id, project = self._env_id(), self._project()
        parts = []
        if env_id:
            pend = self.ctx.roletest.pending_restores(env_id)
            if pend:
                parts.append("The roles of " + ", ".join(sorted({u for _r, _p, u in pend})) + " were changed by an earlier role test and not restored yet.")
            if project and self.ctx.roletest.leftovers(env_id, project):
                parts.append(f"{self.ctx.roletest.leftovers(env_id, project)} test object(s) from an earlier run are still in the project.")
        self.banner.setText("  ".join(parts))
        self.btn_restore.setVisible(bool(env_id and self.ctx.roletest.pending_restores(env_id)))
        self.btn_cleanup.setVisible(bool(env_id and project and self.ctx.roletest.leftovers(env_id, project)))

    # ------------------------------------------------------------------ loading what Polarion knows
    def _load(self) -> None:
        env_id, project = self._env_id(), self._project()
        if not env_id or not project or self._busy:
            self.status.setText("Choose an environment (connected on 'Connect & Test') and a project first.")
            return
        self._begin("Reading users, roles and Work Item types from Polarion...")

        def work(_progress):
            svc = self.ctx.roletest
            return svc.users(env_id), svc.project_roles(env_id, project), svc.work_item_types(env_id, project), svc.document_spaces(env_id, project), svc.assign_capability(env_id, project)

        run_in_background(work, self._loaded, self._failed, on_finished=self._end)

    def _loaded(self, data) -> None:
        users, roles, types, spaces, cap = data
        self._users = users
        self.users.clear()
        for u in users:
            self.users.addItem(f"{u.id}  -  {u.name}" if u.name != u.id else u.id)
        self.roles.clear()
        for r in roles:
            it = QListWidgetItem(r)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.roles.addItem(it)
        self.types.clear()
        for t in types:
            it = QListWidgetItem(t.name)
            it.setData(Qt.UserRole, t)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.types.addItem(it)
        self.space_box.clear()
        self.space_box.addItems(spaces)
        i = self.space_box.findText("Requirements")
        self.space_box.setCurrentIndex(max(i, 0))
        self._cap = cap
        self.cap_note.setText("" if cap.automatic else "Automatic assignment is not possible here: " + cap.reason)
        if not cap.automatic:
            self.mode_have.setChecked(True)
        self.mode_auto.setEnabled(cap.automatic)
        self.status.setText(f"{len(users)} users, {len(roles)} roles and {len(types)} Work Item types found. Choose a user and role(s).")
        self._update_banner()

    def _user_chosen(self, row: int) -> None:
        if row < 0 or row >= len(self._users):
            return
        uid, env_id, project = self._users[row].id, self._env_id(), self._project()
        self.user_now.setText("Current roles: reading...")

        def work(_p):
            return self.ctx.roletest.current_roles(env_id, project, uid)
        run_in_background(work, lambda roles: self._show_current(uid, roles), lambda m: self.user_now.setText("Current roles: could not be read"))

    def _show_current(self, uid: str, roles: list[str]) -> None:
        row = self.users.currentRow()
        if 0 <= row < len(self._users) and self._users[row].id == uid:
            self.user_now.setText(f"{uid} currently has: {', '.join(roles) or 'no role in this project'}")
            if self.mode_have.isChecked():
                for i in range(self.roles.count()):
                    it = self.roles.item(i)
                    it.setCheckState(Qt.Checked if it.text() in roles else Qt.Unchecked)

    def _mode_changed(self) -> None:
        self.restore.setEnabled(self.mode_auto.isChecked())
        self.roles.setEnabled(True)

    # ------------------------------------------------------------------ expected permissions (Excel)
    def _load_sheet(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Expected permissions", "", "Excel (*.xlsx *.xlsm)")
        if not path:
            return
        try:
            names = self.ctx.roletest.sheet_names(path)
        except PavsError as exc:
            QMessageBox.warning(self, "Excel", str(exc))
            return
        self._sheet_path = path
        self.sheet_box.blockSignals(True)
        self.sheet_box.clear()
        self.sheet_box.addItems(names)
        pref = next((i for i, n in enumerate(names) if "proposed" in n.lower()), 0)
        self.sheet_box.setCurrentIndex(pref)
        self.sheet_box.blockSignals(False)
        self.sheet_box.setVisible(len(names) > 1)
        self.btn_sheet_clear.setVisible(True)
        self._sheet_chosen()

    def _sheet_chosen(self) -> None:
        try:
            self.matrix = self.ctx.roletest.load_matrix(self._sheet_path, self.sheet_box.currentText() or None)
        except PavsError as exc:
            self.matrix = None
            self.sheet_label.setText(f"That sheet could not be used: {exc}")
            return
        self.sheet_label.setText(f"Expected results come from sheet '{self.matrix.sheet}': roles {', '.join(self.matrix.roles)}. "
                                 "True = must work, False/empty = must be refused.")

    def _clear_sheet(self) -> None:
        self.matrix, self._sheet_path = None, ""
        self.sheet_box.setVisible(False)
        self.btn_sheet_clear.setVisible(False)
        self.sheet_label.setText("No Excel sheet loaded: every action the test tries is expected to work.")

    # ------------------------------------------------------------------ running
    def _begin(self, message: str) -> None:
        self._busy = True
        for b in (self.btn_load, self.btn_run, self.btn_restore, self.btn_cleanup):
            b.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText(message)

    def _end(self) -> None:
        self._busy = False
        for b in (self.btn_load, self.btn_run, self.btn_restore, self.btn_cleanup):
            b.setEnabled(True)
        self.progress.setVisible(False)
        self._update_banner()

    def _failed(self, message: str) -> None:
        self.status.setText(message)
        QMessageBox.warning(self, "Role test", message)

    def _chosen_roles(self) -> list[str]:
        return [self.roles.item(i).text() for i in range(self.roles.count()) if self.roles.item(i).checkState() == Qt.Checked]

    def _chosen_types(self):
        return [self.types.item(i).data(Qt.UserRole) for i in range(self.types.count()) if self.types.item(i).checkState() == Qt.Checked]

    def _run(self) -> None:
        env_id, project = self._env_id(), self._project()
        row = self.users.currentRow()
        if not env_id or not project or row < 0 or row >= len(self._users):
            QMessageBox.information(self, "Role test", "Load the users and choose one first.")
            return
        user, roles, types = self._users[row].id, self._chosen_roles(), self._chosen_types()
        assign = self.mode_auto.isChecked()
        if not self.password.text():
            QMessageBox.information(self, "Role test", f"Enter the password of '{user}': the test signs in as that user.")
            return
        if assign and not roles:
            QMessageBox.information(self, "Role test", "Tick at least one role to give the user.")
            return
        areas = tuple(a for a, c in (("workitems", self.area_wi), ("documents", self.area_doc), ("other", self.area_other)) if c.isChecked())
        if not areas or ("workitems" in areas and not types):
            QMessageBox.information(self, "Role test", "Choose what to test: Work Item types, documents or test runs.")
            return
        confirmation = self._confirm(env_id, project, user, roles, assign)
        if confirmation is False:
            return
        password, restore, space, matrix = self.password.text(), self.restore.isChecked(), self.space_box.currentText() or None, self.matrix
        self._begin("Starting the role test...")
        self.progress.setRange(0, 100)

        def work(progress):
            return self.ctx.roletest.run(env_id, project, user, password, roles, matrix=matrix, assign=assign, types=types if "workitems" in areas else None,
                                         restore=restore, areas=areas, space=space, production_confirmation=confirmation or None, progress=progress)

        run_in_background(work, self._done, self._failed, on_progress=lambda m, p: (self.status.setText(m), self.progress.setValue(p)), on_finished=self._end)

    def _confirm(self, env_id: str, project: str, user: str, roles: list[str], assign: bool):
        """The explicit approval. Returns the production confirmation text ('' if none) or False to cancel."""
        env = self.ctx.environments.get(env_id)
        lines = [f"- Sign in as '{user}' and really create, change, comment on and delete test Work Items and documents in project '{project}' "
                 "(everything is titled [PAVS-ROLE-...] and removed again when the test ends)."]
        if assign:
            lines.insert(0, f"- TEMPORARILY give '{user}' the role(s) {', '.join(roles)} in project '{project}' by committing a change to that project's "
                            "security/user-roles.xml in the repository" + (", and put the original back afterwards." if self.restore.isChecked() else ". The original is NOT restored automatically."))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Run the role test?")
        box.setText(f"This changes things in Polarion project '{project}' on {env.base_url}.")
        box.setInformativeText("\n".join(lines) + "\n\nUse a dedicated TEST project, never your real working project. Nothing outside this project is touched.")
        go = box.addButton("Run the test", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            return False
        if project not in env.test_projects:
            self.ctx.environments.allow_writes_in(env.id, project, getpass.getuser())
            self.ctx.connections.sync_environment(env.id)
        if env.kind == EnvironmentKind.PRODUCTION:
            typed, ok = QInputDialog.getText(self, "Production environment", f"'{env.name}' is marked as PRODUCTION.\nType its exact name to allow this: {env.name}")
            return typed.strip() if ok else False
        return ""

    def _done(self, outcome) -> None:
        self.outcome = outcome
        rep = outcome.report
        c = rep.counts()
        if rep.passed:
            self.summary.setText(f"<b style='color:#1e7a3c'>PASSED</b> - {rep.user} as {', '.join(rep.roles)} can do everything that was expected "
                                 f"({c['pass']} checks passed, {c['unexpected']} unexpected access, {c['untestable']} not testable).")
        else:
            self.summary.setText(f"<b style='color:#b42318'>NOT PASSED</b> - {c['blocker']} blocker(s) for {rep.user} as {', '.join(rep.roles)} "
                                 f"({c['pass']} passed, {c['unexpected']} unexpected access, {c['untestable']} not testable).")
        self.report_text.setPlainText(rep.text())
        rows = sorted(rep.results, key=lambda r: (ORDER[r.status.value], r.group, r.check_id))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, item(STATUS_TEXT[r.status.value], STATUS_STYLE[r.status.value]))
            self.table.setItem(i, 1, item(r.group.replace("Role test: ", "")))
            self.table.setItem(i, 2, item(r.name))
            self.table.setItem(i, 3, item(r.expected))
            self.table.setItem(i, 4, item(r.actual))
            self.table.setItem(i, 5, item(" | ".join(r.evidence)))
        self.tabs.setCurrentIndex(0)
        self.btn_export.setEnabled(True)
        self.btn_copy.setEnabled(True)
        restored = {True: " The user's original roles were restored.", False: " The user's roles were NOT restored: see the report.", None: ""}[outcome.roles_restored]
        self.status.setText("Role test finished and saved to the history." + restored)

    # ------------------------------------------------------------------ report, recovery
    def _export(self) -> None:
        if self.outcome is None:
            return
        rec = self.outcome.record
        path, _ = QFileDialog.getSaveFileName(self, "Export report", f"role-test-{rec.project_id}.xlsx", "Excel (*.xlsx);;PDF (*.pdf);;HTML (*.html);;CSV (*.csv)")
        if not path:
            return
        try:
            out = self.ctx.testing.export(rec.id, path)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Report saved", f"Saved to {out}")

    def _copy(self) -> None:
        from PySide6.QtGui import QGuiApplication
        if self.outcome is not None:
            QGuiApplication.clipboard().setText(self.outcome.report.text())
            self.status.setText("The blockers report was copied to the clipboard.")

    def _restore_pending(self) -> None:
        env_id = self._env_id()
        if not env_id or self._busy:
            return
        self._begin("Restoring the roles...")
        run_in_background(lambda _p: self.ctx.roletest.restore_pending(env_id),
                          lambda n: self.status.setText(f"Restored the roles of {n} user(s)."), self._failed, on_finished=self._end)

    def _cleanup(self) -> None:
        env_id, project = self._env_id(), self._project()
        if not env_id or not project or self._busy:
            return
        if QMessageBox.question(self, "Clean up test data", f"Delete the {self.ctx.roletest.leftovers(env_id, project)} leftover role-test object(s) from '{project}'?\n"
                                "Only objects this tool recorded and tagged are deleted.") != QMessageBox.Yes:
            return
        self._begin("Cleaning up...")
        run_in_background(lambda _p: self.ctx.roletest.cleanup(env_id, project),
                          lambda r: self.status.setText(f"Deleted {r[0]} object(s)." + (f" {len(r[1])} could not be removed." if r[1] else "")),
                          self._failed, on_finished=self._end)
