"""Environment Manager: add, edit, delete, test connectivity, detect version."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from ...app import AppContext
from ...domain.models import Environment, ServerProfile
from ..dialogs.environment_dialog import EnvironmentDialog
from ..widgets import heading, item, make_table, pretty
from ..workers import run_in_background


class EnvironmentsPage(QWidget):
    profile_updated = Signal(str)          # environment id

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self._busy = False

        self.table = make_table(["Name", "Type", "URL", "Auth", "Release", "Last check"])
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.doubleClicked.connect(lambda _: self._edit())

        self.btn_add = QPushButton("Add environment")
        self.btn_add.setObjectName("primary")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("danger")
        self.btn_test = QPushButton("Test connection && detect version")
        self.btn_test.setObjectName("primary")
        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_delete.clicked.connect(self._delete)
        self.btn_test.clicked.connect(self._test)
        bar = QHBoxLayout()
        for b in (self.btn_add, self.btn_edit, self.btn_delete):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.btn_test)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("muted")
        self.details = QTextEdit()
        self.details.setReadOnly(True)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addLayout(bar)
        tl.addWidget(self.table)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.progress_label)
        bl.addWidget(self.progress)
        bl.addWidget(self.details)
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([300, 400])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Environments",
                              "Saved Polarion instances. Connections are read-only; test data is only "
                              "ever written to the test projects you list, after explicit approval."))
        lay.addWidget(split)
        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        envs = self.ctx.environments.list()
        self._envs = envs
        self.table.setRowCount(len(envs))
        for row, env in enumerate(envs):
            prof = self.ctx.connections.latest_profile(env.id)
            rel = prof.version.release if prof and prof.version.release else "-"
            when = prof.connected_at.astimezone().strftime("%Y-%m-%d %H:%M") if prof else "never"
            kind_key = "unsupported" if env.kind.value == "production" else None
            self.table.setItem(row, 0, item(env.name))
            self.table.setItem(row, 1, item(pretty(env.kind.value), kind_key))
            self.table.setItem(row, 2, item(env.base_url))
            self.table.setItem(row, 3, item(pretty(env.auth_method.value)))
            self.table.setItem(row, 4, item(rel))
            self.table.setItem(row, 5, item(when))
        self._update_buttons()

    def selected(self) -> Environment | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self._envs[rows[0].row()]

    def _update_buttons(self) -> None:
        has = self.selected() is not None
        for b in (self.btn_edit, self.btn_delete, self.btn_test):
            b.setEnabled(has and not self._busy)
        self.btn_add.setEnabled(not self._busy)

    def _show_selected(self) -> None:
        self._update_buttons()
        env = self.selected()
        if env is None:
            self.details.clear()
            return
        prof = self.ctx.connections.latest_profile(env.id)
        self.details.setHtml(render_profile(env, prof))

    # ------------------------------------------------------------------ actions
    def _add(self) -> None:
        dlg = EnvironmentDialog(None, None, self)
        if dlg.exec():
            self._save(dlg)

    def _edit(self) -> None:
        env = self.selected()
        if env is None:
            return
        dlg = EnvironmentDialog(env, self.ctx.environments.has_credentials(env.id), self)
        if dlg.exec():
            self._save(dlg)

    def _save(self, dlg: EnvironmentDialog) -> None:
        try:
            self.ctx.environments.save(dlg.result_env, dlg.result_creds)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.refresh()

    def _delete(self) -> None:
        env = self.selected()
        if env is None:
            return
        if QMessageBox.question(self, "Delete environment",
                                f"Delete '{env.name}' and its stored credentials?") == QMessageBox.Yes:
            self.ctx.environments.delete(env.id)
            self.refresh()
            self.details.clear()

    def _test(self) -> None:
        env = self.selected()
        if env is None or self._busy:
            return
        self._busy = True
        self._update_buttons()
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.details.setHtml(f"<p>Connecting to <b>{env.base_url}</b> ...</p>")

        def on_progress(msg: str, pct: int) -> None:
            self.progress_label.setText(msg)
            self.progress.setValue(pct)

        def on_result(profile: ServerProfile) -> None:
            self.details.setHtml(render_profile(env, profile))
            self.profile_updated.emit(env.id)

        def on_error(msg: str) -> None:
            self.details.setHtml(f"<p style='color:#b42318'><b>Connection failed:</b> {msg}</p>")

        def on_finished() -> None:
            self._busy = False
            self.progress.setVisible(False)
            self.progress_label.setText("")
            self.refresh()
            self._select(env.id)

        run_in_background(lambda progress: self.ctx.connections.connect(env, progress),
                          on_result, on_error, on_progress, on_finished)

    def _select(self, env_id: str) -> None:
        for row, e in enumerate(self._envs):
            if e.id == env_id:
                self.table.selectRow(row)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_profile(env: Environment, prof: ServerProfile | None) -> str:
    if prof is None:
        return (f"<h3>{_esc(env.name)}</h3><p>{_esc(env.base_url)}</p>"
                "<p><i>Not checked yet. Click <b>Test connection &amp; detect version</b>.</i></p>")
    v = prof.version
    rows = "".join(
        f"<tr><td>{_esc(s.source)}</td><td><b>{_esc(s.release or '-')}</b></td>"
        f"<td>{int(s.confidence * 100)}%</td><td>{_esc(s.value[:60])}</td></tr>" for s in v.signals)
    prov = "".join(
        f"<tr><td>{p.kind.value.upper()}</td><td><b>{pretty(p.state.value)}</b></td><td>{_esc(p.detail)}</td></tr>"
        for p in prof.providers)
    counts = prof.summary()["capabilities"]
    cap_line = ", ".join(f"{n} {pretty(k).lower()}" for k, n in sorted(counts.items())) or "none"
    warn = "".join(f"<li>{_esc(w)}</li>" for w in prof.warnings)
    err = "".join(f"<li style='color:#b42318'>{_esc(e)}</li>" for e in prof.errors)
    user = prof.current_user.id if prof.current_user else "not signed in"
    projects = ", ".join(p.id for p in prof.projects[:15]) + (" ..." if len(prof.projects) > 15 else "")
    return f"""
    <h3>{_esc(env.name)} &nbsp;<span style='color:#6b7487;font-weight:normal'>{_esc(env.base_url)}</span></h3>
    <p><b>Polarion {v.release or 'unknown'}</b> ({v.internal_version or '-'}) &middot; confidence {int(v.confidence * 100)}%
       &middot; build {_esc(v.build or '-')} &middot; adapter <b>{_esc(prof.adapter)}</b>
       &middot; user <b>{_esc(user)}</b> &middot; checked {prof.connected_at.astimezone():%Y-%m-%d %H:%M}</p>
    <p><b>Version signals</b></p>
    <table cellpadding=4 border=0><tr><th align=left>Source</th><th>Release</th><th>Weight</th><th align=left>Value</th></tr>{rows}</table>
    <p><b>Providers</b></p>
    <table cellpadding=4><tr><th align=left>Provider</th><th align=left>State</th><th align=left>Detail</th></tr>{prov}</table>
    <p><b>Capabilities:</b> {cap_line} (see Compatibility Center)</p>
    <p><b>Projects ({len(prof.projects)}):</b> {_esc(projects) or '-'}</p>
    {'<p><b>Warnings</b></p><ul>' + warn + '</ul>' if warn else ''}
    {'<p><b>Errors</b></p><ul>' + err + '</ul>' if err else ''}
    <p style='color:#6b7487'>{len(prof.exchanges)} HTTP exchanges recorded as evidence (secrets redacted).</p>
    """
