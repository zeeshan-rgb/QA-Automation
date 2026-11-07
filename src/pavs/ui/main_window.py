"""Main window: sidebar navigation + pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QFrame, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QScrollArea, QStackedWidget, QHBoxLayout, QVBoxLayout, QWidget)

from .. import APP_NAME, APP_TAGLINE, __version__
from ..app import AppContext
from ..services.roles import AppRole, RoleDenied
from .pages.compatibility import CompatibilityPage
from .pages.connect_test import ConnectTestPage
from .pages.dashboard import DashboardPage
from .pages.environments import EnvironmentsPage
from .pages.inspector import InspectorPage
from .pages.merge import MergePage


def r_desc(role: AppRole) -> str:
    from ..services.roles import DESCRIPTION
    return DESCRIPTION[role]


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setMinimumSize(900, 600)
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is not None:
            self.resize(min(1320, int(avail.width() * 0.94)), min(860, int(avail.height() * 0.92)))
            self.move(avail.x() + (avail.width() - self.width()) // 2, avail.y() + (avail.height() - self.height()) // 2)
        else:
            self.resize(1320, 820)

        self.connect_test = ConnectTestPage(ctx)
        self.dashboard = DashboardPage(ctx)
        self.environments = EnvironmentsPage(ctx)
        self.compatibility = CompatibilityPage(ctx)
        self.inspector = InspectorPage(ctx)
        self.merge = MergePage(ctx)
        pages: list[tuple[str, QWidget]] = [
            ("Connect & Test", self.connect_test),
            ("Dashboard", self.dashboard),
            ("Configuration Inspector", self.inspector),
            ("Merge & Templates", self.merge),
            ("Compatibility Center", self.compatibility),
            ("Environments", self.environments),
        ]

        self.stack = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for name, page in pages:
            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QFrame.NoFrame)
            scroller.setWidget(page)
            self.stack.addWidget(scroller)
            QListWidgetItem(name, self.nav)
        self.nav.currentRowChanged.connect(self._go)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 12)
        brand = QLabel("Polarion ALM\nValidation Studio")
        brand.setObjectName("brand")
        tag = QLabel(APP_TAGLINE)
        tag.setObjectName("tagline")
        tag.setWordWrap(True)
        sl.addWidget(brand)
        sl.addWidget(tag)
        sl.addWidget(self.nav, 1)
        role_label = QLabel("Role")
        role_label.setObjectName("tagline")
        self.role_box = QComboBox()
        for r in AppRole:
            self.role_box.addItem(r.label, r)
            self.role_box.setItemData(self.role_box.count() - 1, r_desc(r), Qt.ToolTipRole)
        self._show_role()
        self.role_box.currentIndexChanged.connect(self._role_chosen)
        sl.addWidget(role_label)
        sl.addWidget(self.role_box)
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("tagline")
        sl.addWidget(ver)

        central = QWidget()
        cl = QHBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(sidebar)
        cl.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        vault = "Windows Credential Manager" if ctx.vault.persistent else "session only (no secure store found)"
        self.statusBar().showMessage(f"Read-only unless you approve test data in a test project  |  Credentials: {vault}  |  Data: {ctx.db.path}")
        self.environments.profile_updated.connect(self._profile_updated)
        self.nav.setCurrentRow(0)

    def _show_role(self) -> None:
        self.role_box.blockSignals(True)
        self.role_box.setCurrentIndex(list(AppRole).index(self.ctx.roles.current))
        self.role_box.blockSignals(False)
        self.role_box.setToolTip(r_desc(self.ctx.roles.current))

    def _role_chosen(self, _index: int) -> None:
        role = self.role_box.currentData()
        passphrase = ""
        if role.rank > self.ctx.roles.current.rank and self.ctx.roles.passphrase_set:
            passphrase, ok = QInputDialog.getText(self, "Role passphrase", f"Passphrase to switch to {role.label}:",
                                                  QLineEdit.Password)
            if not ok:
                self._show_role()
                return
        self.apply_role(role, passphrase)

    def apply_role(self, role: AppRole, passphrase: str = "") -> bool:
        try:
            self.ctx.roles.set_role(role, passphrase)
        except RoleDenied as exc:
            QMessageBox.warning(self, "Role", str(exc))
            self._show_role()
            return False
        self._show_role()
        self.statusBar().showMessage(f"Role: {role.label}. {r_desc(role)}", 8000)
        return True

    def _go(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        scroller = self.stack.currentWidget()
        page = scroller.widget() if isinstance(scroller, QScrollArea) else scroller
        if hasattr(page, "refresh"):
            page.refresh()

    def _profile_updated(self, env_id: str) -> None:
        self.dashboard.refresh()
        self.compatibility.refresh()
        self.inspector.refresh()
        self.merge.refresh()
        self.connect_test.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.ctx.close()
        super().closeEvent(event)
