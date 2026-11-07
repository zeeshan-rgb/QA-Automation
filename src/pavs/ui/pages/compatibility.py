"""Compatibility Center: detected version, live capability matrix, documented release matrix."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QTabWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from ...app import AppContext
from ...services.compatibility import CompatibilityService
from ..widgets import heading, item, make_table, pretty


class CompatibilityPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.compat = CompatibilityService(ctx.registry)

        self.env_box = QComboBox()
        self.env_box.currentIndexChanged.connect(self._show_live)
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses", None)
        for s in ("supported", "partial", "needs_permission", "unsupported", "unknown"):
            self.status_filter.addItem(pretty(s), s)
        self.status_filter.currentIndexChanged.connect(self._show_live)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        self.live = make_table(["Area", "Capability", "Status", "Verification", "Provider", "Detail"])
        live_w = QWidget()
        ll = QVBoxLayout(live_w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Environment"))
        row.addWidget(self.env_box, 1)
        row.addWidget(self.status_filter)
        ll.addLayout(row)
        ll.addWidget(self.summary)
        ll.addWidget(self.live)

        self.matrix = make_table(["Area", "Capability"], stretch_last=False)
        self.adapters = QTextEdit()
        self.adapters.setReadOnly(True)

        tabs = QTabWidget()
        tabs.addTab(live_w, "This server")
        tabs.addTab(self.matrix, "Release matrix (documented)")
        tabs.addTab(self.adapters, "Adapters && quirks")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Compatibility Center",
                              "Capability detection beats version numbers: live REST spec, WSDL and "
                              "probes decide first, the documented catalog only fills gaps. 'Verified' "
                              "means a real call on this server returned data."))
        lay.addWidget(tabs)
        self._fill_matrix()
        self._fill_adapters()
        self.refresh()

    def refresh(self) -> None:
        current = self.env_box.currentData()
        self.env_box.blockSignals(True)
        self.env_box.clear()
        for env in self.ctx.environments.list():
            self.env_box.addItem(env.name, env.id)
        idx = self.env_box.findData(current)
        self.env_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.env_box.blockSignals(False)
        self._show_live()

    def _show_live(self) -> None:
        env_id = self.env_box.currentData()
        prof = self.ctx.connections.latest_profile(env_id) if env_id else None
        if prof is None:
            self.live.setRowCount(0)
            self.summary.setText("No connection result yet. Test the connection on the Environments page.")
            return
        v = prof.version
        self.summary.setText(
            f"Polarion {v.release or 'unknown'} ({v.internal_version or '-'}), adapter {prof.adapter}, "
            f"checked {prof.connected_at.astimezone():%Y-%m-%d %H:%M}. "
            + " ".join(prof.warnings))
        want = self.status_filter.currentData()
        recs = [c for c in prof.capabilities if want is None or c.status.value == want]
        self.live.setRowCount(len(recs))
        for r, c in enumerate(sorted(recs, key=lambda c: (c.area, c.capability_id))):
            self.live.setItem(r, 0, item(c.area))
            self.live.setItem(r, 1, item(c.title, tooltip=c.capability_id))
            self.live.setItem(r, 2, item(pretty(c.status.value), c.status.value))
            self.live.setItem(r, 3, item(pretty(c.verification.value), c.verification.value))
            self.live.setItem(r, 4, item(c.provider.value.upper() if c.provider else "-"))
            self.live.setItem(r, 5, item(c.detail, tooltip=c.detail))

    def _fill_matrix(self) -> None:
        scenarios, rows = self.compat.documented_matrix()
        headers = ["Area", "Capability"] + [s.label for s in scenarios]
        self.matrix.setColumnCount(len(headers))
        self.matrix.setHorizontalHeaderLabels(headers)
        self.matrix.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self.matrix.setItem(r, 0, item(row.area))
            self.matrix.setItem(r, 1, item(row.title, tooltip=row.notes or row.capability_id))
            for c, sc in enumerate(scenarios, start=2):
                status, provider = row.cells.get(sc.key, (None, ""))
                if provider == "svn opt-in":
                    text = "SVN reader (opt-in)"
                elif status and provider:
                    text = f"{pretty(status.value)} ({provider.upper()})"
                else:
                    text = pretty(status.value) if status else "-"
                self.matrix.setItem(r, c, item(text, status.value if status else None))
        from PySide6.QtWidgets import QHeaderView
        self.matrix.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

    def _fill_adapters(self) -> None:
        html = ["<h3>Registered adapters</h3><p>Only real Polarion releases have adapters. 2503 and 2603 "
                "do not appear in Siemens' SDK change log; a server reporting another release uses the "
                "nearest lower adapter plus live probing, and newer releases use the Future adapter.</p>"]
        for a in self.compat.adapters_info():
            quirks = "".join(f"<li>{q}</li>" for q in a["quirks"]) or "<li>none recorded</li>"
            state = "validated" if a["validated"] else "not yet validated on a real server"
            html.append(f"<p><b>{a['name']}</b> - {state}</p><ul>{quirks}</ul>")
        self.adapters.setHtml("".join(html))
