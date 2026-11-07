"""Merge & Templates: compare two projects' configuration, check what would arrive, hand the merge to TortoiseSVN."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from ...app import AppContext
from ...domain.errors import PavsError
from ...services.merge import FileDiff, MergeReport
from ..widgets import heading, item, make_table
from ..workers import run_in_background

STATE_KEY = {"added": "supported", "changed": "partial", "removed": "unsupported", "same": "documented"}
SEVERITY_KEY = {"error": "unsupported", "warning": "partial", "info": "documented", "": "supported"}


class MergePage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.report: MergeReport | None = None
        self._rows: list[FileDiff] = []

        self.src_env, self.src_proj = QComboBox(), QComboBox()
        self.dst_env, self.dst_proj = QComboBox(), QComboBox()
        for box in (self.src_env, self.src_proj, self.dst_env, self.dst_proj):
            box.setMinimumContentsLength(14)
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.src_env.currentIndexChanged.connect(lambda _: self._fill_projects(self.src_env, self.src_proj))
        self.dst_env.currentIndexChanged.connect(lambda _: self._fill_projects(self.dst_env, self.dst_proj))

        grid = QGridLayout()
        grid.addWidget(QLabel("Source (what you merge FROM)"), 0, 0, 1, 2)
        grid.addWidget(QLabel("Target (what you merge INTO)"), 0, 2, 1, 2)
        grid.addWidget(self.src_env, 1, 0)
        grid.addWidget(self.src_proj, 1, 1)
        grid.addWidget(self.dst_env, 1, 2)
        grid.addWidget(self.dst_proj, 1, 3)
        self.btn_compare = QPushButton("Compare and check")
        self.btn_compare.setObjectName("primary")
        self.btn_compare.clicked.connect(self._compare)
        grid.addWidget(self.btn_compare, 1, 4)

        self.tortoise = QLabel()
        self.tortoise.setObjectName("muted")
        self.tortoise.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("Compare two projects (on the same or different Polarion servers). Read-only: nothing is changed.")
        self.status.setWordWrap(True)
        self.notes = QLabel()
        self.notes.setObjectName("muted")
        self.notes.setWordWrap(True)

        self.table = make_table(["State", "Area", "File", "Problems", "First problem"])
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.btn_compare_ts = QPushButton("Compare in TortoiseSVN")
        self.btn_diff = QPushButton("Diff selected file")
        self.btn_log = QPushButton("Log of selected")
        self.btn_browse = QPushButton("Repository browser")
        self.btn_checkout = QPushButton("Check out target...")
        self.btn_merge = QPushButton("Merge in working copy...")
        self.btn_export = QPushButton("Export report...")
        self.btn_compare_ts.clicked.connect(lambda: self._launch(lambda: self.ctx.merge.open_compare(self.report)))
        self.btn_diff.clicked.connect(self._diff)
        self.btn_log.clicked.connect(self._log)
        self.btn_browse.clicked.connect(lambda: self._launch(lambda: self.ctx.merge.open_repo_browser(self.report.target_root_url)))
        self.btn_checkout.clicked.connect(self._checkout)
        self.btn_merge.clicked.connect(self._merge)
        self.btn_export.clicked.connect(self._export)
        actions = QHBoxLayout()
        for b in (self.btn_compare_ts, self.btn_diff, self.btn_log, self.btn_browse, self.btn_checkout, self.btn_merge):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.btn_export)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Merge & Templates",
                              "Compare two projects' configuration and templates, see what would break on the target release, "
                              "then finish the merge in TortoiseSVN. This app never commits or merges by itself."))
        lay.addLayout(grid)
        lay.addWidget(self.tortoise)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.notes)
        lay.addLayout(actions)
        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        envs = self.ctx.environments.list()
        for box in (self.src_env, self.dst_env):
            current = box.currentData()
            box.blockSignals(True)
            box.clear()
            for e in envs:
                box.addItem(e.name, e.id)
            idx = box.findData(current)
            box.setCurrentIndex(max(idx, 0))
            box.blockSignals(False)
        self._fill_projects(self.src_env, self.src_proj)
        self._fill_projects(self.dst_env, self.dst_proj)
        if self.ctx.merge.tortoise_available:
            self.tortoise.setText(f"TortoiseSVN found: {self.ctx.merge.launcher.info.proc}")
        else:
            self.tortoise.setText("TortoiseSVN was not found. You can still compare and check; install TortoiseSVN "
                                  "(tortoisesvn.net) to open the compare, diff and merge dialogs from here.")
        self._update_buttons()

    def _fill_projects(self, env_box: QComboBox, proj_box: QComboBox) -> None:
        current = proj_box.currentText()
        proj_box.clear()
        env_id = env_box.currentData()
        prof = self.ctx.connections.latest_profile(env_id) if env_id else None
        for p in (prof.projects if prof else []):
            proj_box.addItem(p.id)
        i = proj_box.findText(current)
        if i >= 0:
            proj_box.setCurrentIndex(i)

    def _update_buttons(self) -> None:
        have = self.report is not None
        ts = self.ctx.merge.tortoise_available
        selected = self._selected() is not None
        for b in (self.btn_compare_ts, self.btn_browse, self.btn_checkout, self.btn_merge):
            b.setEnabled(have and ts)
        self.btn_diff.setEnabled(have and ts and selected)
        self.btn_log.setEnabled(have and ts and selected)
        self.btn_export.setEnabled(have)

    def _selected(self) -> FileDiff | None:
        r = self.table.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) else None

    # ------------------------------------------------------------------ compare
    def _compare(self) -> None:
        s_env, s_proj, d_env, d_proj = (self.src_env.currentData(), self.src_proj.currentText(),
                                        self.dst_env.currentData(), self.dst_proj.currentText())
        if not (s_env and s_proj and d_env and d_proj):
            QMessageBox.information(self, "Merge", "Choose a source and a target project. Connect to the server first "
                                                   "(Connect & Test) so its projects are listed.")
            return
        self.btn_compare.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)

        def done(rep: MergeReport) -> None:
            self.report = rep
            self._show(rep)

        def finished() -> None:
            self.btn_compare.setEnabled(True)
            self.progress.setVisible(False)

        run_in_background(lambda progress: self.ctx.merge.compare(s_env, s_proj, d_env, d_proj, progress=progress),
                          done, lambda msg: (self.status.setText(msg), QMessageBox.warning(self, "Compare failed", msg)),
                          lambda msg, pct: (self.status.setText(msg), self.progress.setValue(pct)), finished)

    def _show(self, rep: MergeReport) -> None:
        self._rows = sorted(rep.files, key=lambda f: ({"error": 0, "warning": 1, "info": 2, "": 3}[f.worst], f.state, f.path))
        self.table.setRowCount(len(self._rows))
        for r, f in enumerate(self._rows):
            first = f.findings[0] if f.findings else None
            self.table.setItem(r, 0, item(f.state.upper(), STATE_KEY[f.state]))
            self.table.setItem(r, 1, item(f.area))
            self.table.setItem(r, 2, item(f.path))
            self.table.setItem(r, 3, item(str(len(f.findings)) if f.findings else "", SEVERITY_KEY[f.worst] if f.findings else None))
            self.table.setItem(r, 4, item(f"{first.rule} line {first.line}: {first.message}" if first else "",
                                          None, "\n".join(f"{x.rule} line {x.line}: {x.message}" for x in f.findings[:10]) or None))
        c = rep.counts()
        head = (f"{rep.source_label} (Polarion {rep.source_release or '?'}) -> {rep.target_label} (Polarion {rep.target_release or '?'}): "
                f"{c['added']} new, {c['changed']} changed, {c['removed']} only in the target.")
        if rep.ready:
            self.status.setText(head + " Nothing found that would break the target.")
        else:
            self.status.setText(f"{head} {len(rep.blockers)} problem(s) in incoming files would break the target. Fix them in the source first.")
        self.notes.setText("\n".join(rep.notes))
        self._update_buttons()

    # ------------------------------------------------------------------ TortoiseSVN
    def _launch(self, fn) -> None:
        try:
            fn()
        except PavsError as exc:
            QMessageBox.warning(self, "TortoiseSVN", str(exc))

    def _diff(self) -> None:
        f = self._selected()
        if f:
            self._launch(lambda: self.ctx.merge.open_file_diff(self.report, f))

    def _log(self) -> None:
        f = self._selected()
        if f:
            self._launch(lambda: self.ctx.merge.open_log(f.source_url or f.target_url))

    def _checkout(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Empty folder for the working copy of the TARGET configuration")
        if folder:
            self._launch(lambda: self.ctx.merge.checkout_target(self.report, folder))

    def _merge(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Working copy of the target (checked out earlier)")
        if not folder or self.report is None:
            return
        ok = QMessageBox.question(
            self, "Merge in TortoiseSVN",
            "TortoiseSVN's merge wizard opens on this working copy. Nothing is committed until you review the result and commit it "
            "yourself.\n\n" + ("This comparison found problems in incoming files. " if not self.report.ready else "") + "Continue?")
        if ok == QMessageBox.Yes:
            self._launch(lambda: self.ctx.merge.open_merge(folder, self.report.target_env_id))

    def _export(self) -> None:
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export merge report", "merge-check.xlsx",
                                              "Excel (*.xlsx);;PDF (*.pdf);;HTML (*.html);;CSV (*.csv)")
        if not path:
            return
        try:
            out = self.ctx.testing.export(self.report.run_id, path)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Report saved", f"Saved to {out}")
