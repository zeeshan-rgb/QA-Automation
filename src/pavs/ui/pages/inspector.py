"""Configuration Inspector: read a project's configuration, browse it, compare snapshots, check permissions."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ...app import AppContext
from ...domain.config import ConfigSnapshot, SectionResult
from ..theme import STATUS_COLORS
from ..widgets import heading, item, make_table, pretty
from ..workers import run_in_background

# columns shown for each named list (anything else falls back to the item's simple keys)
COLUMNS: dict[str, list[tuple[str, str]]] = {
    "types": [("id", "ID"), ("name", "Name"), ("default", "Default"), ("sort_order", "Order")],
    "link_roles": [("id", "ID"), ("name", "Name"), ("opposite_name", "Opposite"), ("parent", "Parent link"),
                   ("rules", "Allowed (from, to)")],
    "effective": [("target_type", "Type"), ("id", "ID"), ("name", "Name"), ("type", "Field type"),
                  ("required", "Required"), ("multi", "Multi"), ("prototype", "Prototype"),
                  ("scope", "Scope"), ("initial_status", "Initial status"), ("sources", "Source")],
    "layers": [("scope", "Scope"), ("target_type", "Type"), ("id", "ID"), ("name", "Name"), ("type", "Field type"),
               ("required", "Required"), ("sources", "File")],
    "enumerations": [("enum_id", "Enumeration"), ("target_type", "Type"), ("scope", "Scope"),
                     ("options", "Options"), ("sources", "Source")],
    "rules": [("scope", "Scope"), ("role", "Role"), ("effect", "Effect"), ("permission", "Permission"),
              ("view", "View"), ("kind", "Rule"), ("target_type", "Type"), ("ref", "Status / action"),
              ("fields", "Fields"), ("excluded", "Hidden")],
    "customsets": [("scope", "Scope"), ("id", "ID"), ("title", "Title"), ("query", "Query")],
    "roles": [("scope", "Scope"), ("id", "Role"), ("sources", "Source")],
    "assignments": [("scope", "Scope"), ("principal", "User / group"), ("principal_kind", "Kind"),
                    ("role", "Role")],
    "files": [("prototype", "Prototype"), ("scope", "Scope"), ("target_type", "Type"),
              ("initial_status", "Initial status"), ("actions", "Actions"), ("sources", "File")],
    "item_actions": [("work_item_type", "Type"), ("work_item", "Item"), ("current_status", "Status"),
                     ("action_id", "Action"), ("name", "Name"), ("available", "Available"),
                     ("target_status", "Target"), ("required_fields", "Required fields"),
                     ("unavailable_reason", "Why not available")],
    "documents": [("space", "Space"), ("name", "Name"), ("title", "Title"), ("type", "Type"), ("status", "Status")],
    "document_types": [("id", "ID"), ("name", "Name"), ("default", "Default")],
    "document_statuses": [("id", "ID"), ("name", "Name"), ("default", "Default")],
    "pages": [("space", "Space"), ("name", "Name"), ("title", "Title"), ("kind", "Kind")],
}
SECTION_STATUS_KEY = {"complete": "supported", "partial": "partial", "unavailable": "unsupported",
                      "error": "unsupported"}


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            if "location" in value[0]:
                return ", ".join(v.get("location", "") for v in value)
            if "id" in value[0]:
                return f"{len(value)}: " + ", ".join(str(v.get("id")) for v in value[:8]) + (" ..." if len(value) > 8 else "")
            return f"{len(value)} items"
        if value and isinstance(value[0], list):
            return "; ".join("→".join(map(str, v)) for v in value)
        return ", ".join(map(str, value))
    return str(value)


class InspectorPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.snapshot: ConfigSnapshot | None = None
        self.snapshot_id: int | None = None
        self._busy = False

        self.env_box = QComboBox()
        self.env_box.currentIndexChanged.connect(self._env_changed)
        self.project_box = QComboBox()
        self.project_box.setEditable(True)
        self.project_box.currentIndexChanged.connect(self._load_history)
        self.sample = QCheckBox("Sample one Work Item per type (read-only)")
        self.sample.setChecked(True)
        self.btn_inspect = QPushButton("Inspect configuration")
        self.btn_inspect.setObjectName("primary")
        self.btn_inspect.clicked.connect(self._inspect)
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self._load_selected_snapshot)
        self.btn_compare = QPushButton("Compare with previous")
        self.btn_compare.clicked.connect(self._compare)

        top = QHBoxLayout()
        for lbl, w in (("Environment", self.env_box), ("Project", self.project_box)):
            top.addWidget(QLabel(lbl))
            top.addWidget(w, 1)
        top.addWidget(self.sample)
        top.addWidget(self.btn_inspect)
        hist = QHBoxLayout()
        hist.addWidget(QLabel("Snapshot"))
        hist.addWidget(self.history, 1)
        hist.addWidget(self.btn_compare)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        self.sections = QListWidget()
        self.sections.setMinimumWidth(260)
        self.sections.setSpacing(3)
        self.sections.currentRowChanged.connect(self._show_section)
        self.tabs = QTabWidget()
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(200)

        # permission check tool
        self.perm_user = QLineEdit()
        self.perm_user.setPlaceholderText("user id, e.g. reviewer1")
        self.perm_name = QLineEdit()
        self.perm_name.setPlaceholderText("permission, e.g. com.polarion.persistence.object.WorkItem.modify")
        self.btn_perm = QPushButton("Check")
        self.btn_perm.clicked.connect(self._check_permission)
        self.perm_result = QLabel("")
        perm_box = QGroupBox("Check a permission on the server (SOAP Security.hasPermission, read-only)")
        pl = QHBoxLayout(perm_box)
        pl.addWidget(self.perm_user, 1)
        pl.addWidget(self.perm_name, 3)
        pl.addWidget(self.btn_perm)
        pl.addWidget(self.perm_result, 1)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tabs, 1)
        rl.addWidget(self.detail)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.sections)
        split.addWidget(right)
        split.setSizes([280, 1000])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Configuration Inspector",
                              "Reads a project's configuration through REST, SOAP and (if enabled) the SVN reader. "
                              "Nothing is changed on the server. Gaps say what could not be read and why."))
        lay.addLayout(top)
        lay.addLayout(hist)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addWidget(split, 1)
        lay.addWidget(perm_box)
        self.refresh()

    # ------------------------------------------------------------------ selectors
    def refresh(self) -> None:
        current = self.env_box.currentData()
        self.env_box.blockSignals(True)
        self.env_box.clear()
        for env in self.ctx.environments.list():
            self.env_box.addItem(env.name, env.id)
        idx = self.env_box.findData(current)
        self.env_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.env_box.blockSignals(False)
        self._env_changed()

    def _env_changed(self) -> None:
        env_id = self.env_box.currentData()
        prof = self.ctx.connections.latest_profile(env_id) if env_id else None
        current = self.project_box.currentText()
        self.project_box.blockSignals(True)
        self.project_box.clear()
        for p in (prof.projects if prof else []):
            self.project_box.addItem(p.id, p.id)
        if current and self.project_box.findText(current) >= 0:
            self.project_box.setCurrentText(current)
        self.project_box.blockSignals(False)
        if prof is None:
            self.status.setText("Connect to this environment first (Environments page).")
        elif not prof.projects:
            self.status.setText("No projects were discovered on the last connection: type a project id.")
        else:
            self.status.setText(f"Polarion {prof.version.release or '?'} - providers: " + ", ".join(
                f"{p.kind.value.upper()} {p.state.value.replace('_', ' ')}" for p in prof.providers))
        self._load_history()

    def _project(self) -> str:
        return (self.project_box.currentText() or "").strip()

    def _load_history(self) -> None:
        env_id, project = self.env_box.currentData(), self._project()
        self.history.blockSignals(True)
        self.history.clear()
        if env_id and project:
            for sid, when, release in self.ctx.inspection.history(env_id, project):
                self.history.addItem(f"#{sid}  {when[:19].replace('T', ' ')}  (Polarion {release or '?'})", sid)
        self.history.blockSignals(False)
        self._load_selected_snapshot()

    def _load_selected_snapshot(self) -> None:
        sid = self.history.currentData()
        snap = self.ctx.inspection.load(sid) if sid else None
        self.snapshot, self.snapshot_id = snap, sid
        self._show_snapshot()

    # ------------------------------------------------------------------ run
    def _inspect(self) -> None:
        env_id, project = self.env_box.currentData(), self._project()
        if not env_id or not project or self._busy:
            QMessageBox.information(self, "Inspect", "Choose an environment and a project first.")
            return
        self._busy = True
        self.btn_inspect.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        sample = self.sample.isChecked()

        def done(result) -> None:
            sid, _ = result
            self._load_history()
            idx = self.history.findData(sid)
            if idx >= 0:
                self.history.setCurrentIndex(idx)

        def finished() -> None:
            self._busy = False
            self.btn_inspect.setEnabled(True)
            self.progress.setVisible(False)

        run_in_background(
            lambda progress: self.ctx.inspection.inspect(env_id, project, progress=progress, sample_items=sample),
            done, lambda msg: QMessageBox.warning(self, "Inspection failed", msg),
            lambda msg, pct: (self.status.setText(msg), self.progress.setValue(pct)), finished)

    # ------------------------------------------------------------------ display
    def _show_snapshot(self) -> None:
        self.sections.clear()
        self.tabs.clear()
        self.detail.clear()
        if self.snapshot is None:
            return
        for sec in self.snapshot.sections:
            providers = ", ".join(p.value.upper() for p in sec.providers) or "-"
            it = QListWidgetItem(f"{sec.title}\n{pretty(sec.status.value)} · {sec.count()} items · {providers}")
            bg, fg = STATUS_COLORS[SECTION_STATUS_KEY.get(sec.status.value, "unknown")]
            from PySide6.QtGui import QBrush, QColor
            it.setForeground(QBrush(QColor(fg)))
            it.setBackground(QBrush(QColor(bg)))
            self.sections.addItem(it)
        self.sections.setCurrentRow(0)

    def _show_section(self, row: int) -> None:
        self.tabs.clear()
        self.detail.clear()
        if self.snapshot is None or row < 0 or row >= len(self.snapshot.sections):
            return
        sec = self.snapshot.sections[row]
        for name, items in sec.items.items():
            self.tabs.addTab(self._table(name, items), f"{pretty(name)} ({len(items)})")
        self.tabs.addTab(self._report(sec), f"Sources && gaps ({len(sec.gaps) + len(sec.errors)})")

    def _table(self, name: str, items: list[dict]):
        cols = COLUMNS.get(name)
        if cols:
            cols = [c for c in cols if any(c[0] in i for i in items)] or cols[:1]
        else:
            keys = [k for k, v in (items[0] if items else {}).items() if not isinstance(v, (list, dict))]
            cols = [(k, pretty(k)) for k in keys]
        t = make_table([c[1] for c in cols])
        t.setRowCount(len(items))
        for r, it in enumerate(items):
            for c, (key, _) in enumerate(cols):
                v = it.get(key)
                status = None
                if key == "effect":
                    status = "supported" if v == "grant" else "unsupported"
                elif key == "available":
                    status = "supported" if v else "unsupported"
                    v = "yes" if v else "no"
                t.setItem(r, c, item(cell(v), status, cell(v) if len(cell(v)) > 60 else None))
        t.itemSelectionChanged.connect(lambda tbl=t, data=items: self._show_item(tbl, data))
        return t

    def _show_item(self, table, items: list[dict]) -> None:
        rows = table.selectionModel().selectedRows()
        if rows:
            self.detail.setPlainText(json.dumps(items[rows[0].row()], indent=2, ensure_ascii=False))

    def _report(self, sec: SectionResult) -> QTextEdit:
        out = QTextEdit()
        out.setReadOnly(True)
        parts = [f"<h3>{sec.title}: {pretty(sec.status.value)}</h3>",
                 f"<p>Providers used: {', '.join(p.value.upper() for p in sec.providers) or 'none'} "
                 f"&middot; {sec.duration_ms or 0:.0f} ms</p>"]
        if sec.gaps:
            parts.append("<p><b>Could not be inspected</b></p><ul>" + "".join(
                f"<li><b>{g.item}</b>: {g.reason}" + (f"<br><i>How to fix: {g.remedy}</i>" if g.remedy else "")
                + "</li>" for g in sec.gaps) + "</ul>")
        if sec.notes:
            parts.append("<p><b>Notes</b></p><ul>" + "".join(f"<li>{n}</li>" for n in sec.notes) + "</ul>")
        if sec.errors:
            parts.append("<p><b>Errors</b></p><ul>" + "".join(
                f"<li style='color:#b42318'>{e}</li>" for e in sec.errors) + "</ul>")
        out.setHtml("".join(parts))
        return out

    # ------------------------------------------------------------------ tools
    def _compare(self) -> None:
        if self.history.count() < 2 or self.snapshot_id is None:
            QMessageBox.information(self, "Compare", "Inspect the project at least twice to compare snapshots.")
            return
        idx = self.history.currentIndex()
        if idx + 1 >= self.history.count():
            QMessageBox.information(self, "Compare", "This is the oldest snapshot.")
            return
        old_id = self.history.itemData(idx + 1)
        diffs = self.ctx.inspection.compare(old_id, self.snapshot_id)
        lines = [f"Changes from snapshot #{old_id} to #{self.snapshot_id}", ""]
        for d in diffs:
            if d.empty:
                continue
            lines.append(f"== {d.area}")
            lines += [f"  + {x}" for x in d.added] + [f"  - {x}" for x in d.removed] + [f"  ~ {x}" for x in d.changed]
        if len(lines) == 2:
            lines.append("No differences.")
        dlg = QDialog(self)
        dlg.setWindowTitle("Snapshot comparison")
        dlg.resize(800, 500)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText("\n".join(lines))
        QVBoxLayout(dlg).addWidget(txt)
        dlg.exec()

    def _check_permission(self) -> None:
        env_id, project = self.env_box.currentData(), self._project()
        user, perm = self.perm_user.text().strip(), self.perm_name.text().strip()
        if not (env_id and project and user and perm):
            self.perm_result.setText("Fill in user and permission.")
            return
        self.perm_result.setText("Checking ...")

        def show(r) -> None:
            if r.granted is None:
                self.perm_result.setText(f"Unknown: {r.detail}")
            else:
                self.perm_result.setText(("GRANTED" if r.granted else "DENIED") + f" for {r.user} in {r.project}")

        run_in_background(lambda _: self.ctx.inspection.check_permission(env_id, project, user, perm),
                          show, lambda msg: self.perm_result.setText(msg))
