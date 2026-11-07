"""Colours and the application style sheet."""

NAVY = "#14213d"
NAVY_2 = "#1f2f52"
ACCENT = "#2f6fed"
BG = "#f4f6fa"
CARD = "#ffffff"
TEXT = "#1d2433"
MUTED = "#6b7487"
BORDER = "#dde2ec"

STATUS_COLORS = {
    "supported": ("#e7f6ec", "#1e7a3c"),
    "partial": ("#fff6e0", "#946200"),
    "unsupported": ("#fdeaea", "#b42318"),
    "needs_permission": ("#eaf1ff", "#2050b8"),
    "unknown": ("#f0f1f4", "#5b6475"),
    # provider states
    "authenticated": ("#e7f6ec", "#1e7a3c"),
    "available": ("#eaf1ff", "#2050b8"),
    "auth_failed": ("#fdeaea", "#b42318"),
    "disabled": ("#f0f1f4", "#5b6475"),
    "unreachable": ("#fdeaea", "#b42318"),
    "not_configured": ("#f0f1f4", "#5b6475"),
    # verification
    "verified": ("#e7f6ec", "#1e7a3c"),
    "detected": ("#eaf1ff", "#2050b8"),
    "documented": ("#f0f1f4", "#5b6475"),
}

STYLE = f"""
QMainWindow, QWidget#page {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-family: 'Segoe UI'; font-size: 10pt; }}
QFrame#sidebar {{ background: {NAVY}; }}
QLabel#brand {{ color: white; font-size: 13pt; font-weight: 600; padding: 18px 16px 2px 16px; }}
QLabel#tagline {{ color: #aab4cc; font-size: 8pt; padding: 0 16px 16px 16px; }}
QListWidget#nav {{ background: transparent; border: none; color: #dfe5f2; outline: 0; }}
QListWidget#nav::item {{ padding: 10px 16px; border-left: 3px solid transparent; }}
QListWidget#nav::item:selected {{ background: {NAVY_2}; color: white; border-left: 3px solid {ACCENT}; }}
QListWidget#nav::item:hover {{ background: {NAVY_2}; }}
QLabel#h1 {{ font-size: 16pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel#cardValue {{ font-size: 20pt; font-weight: 600; }}
QLabel#cardLabel {{ color: {MUTED}; font-size: 9pt; }}
QPushButton {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: 1px solid {ACCENT}; }}
QPushButton#primary:disabled {{ background: #9db7f3; border-color: #9db7f3; }}
QPushButton#danger {{ color: #b42318; }}
QTableWidget, QTreeWidget, QTextEdit, QPlainTextEdit {{ background: {CARD}; border: 1px solid {BORDER};
    border-radius: 6px; gridline-color: {BORDER}; selection-background-color: #dce7ff; selection-color: {TEXT}; }}
QHeaderView::section {{ background: #eef1f7; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px; font-weight: 600; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: white; border: 1px solid {BORDER};
    border-radius: 5px; padding: 4px 6px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 5px; background: white; height: 14px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; background: {CARD}; }}
QTabBar::tab {{ background: #e6eaf3; color: {TEXT}; padding: 7px 16px; border: 1px solid {BORDER};
    border-bottom: none; margin-right: 2px; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
QTabBar::tab:selected {{ background: {CARD}; color: {ACCENT}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ background: #dde4f2; }}
QComboBox QAbstractItemView {{ background: white; color: {TEXT}; selection-background-color: #dce7ff;
    selection-color: {TEXT}; border: 1px solid {BORDER}; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid #9aa4b8; border-radius: 3px; background: white; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ color: {TEXT}; }}
QToolTip {{ background: #ffffe8; color: #1d2433; border: 1px solid {BORDER}; }}
QScrollArea {{ background: transparent; border: none; }}
QSplitter::handle {{ background: {BORDER}; }}
QMessageBox, QDialog {{ background: {BG}; }}
QStatusBar {{ background: white; border-top: 1px solid {BORDER}; }}
"""


def apply_theme(app) -> None:
    """Force the same light look on every PC, including Windows dark mode (the native dark palette
    made tabs and checkboxes unreadable)."""
    from PySide6.QtGui import QColor, QPalette

    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: "#ffffff", QPalette.AlternateBase: "#f0f3f9",
        QPalette.Text: TEXT, QPalette.Button: "#ffffff", QPalette.ButtonText: TEXT, QPalette.ToolTipBase: "#ffffe8",
        QPalette.ToolTipText: TEXT, QPalette.Highlight: "#dce7ff", QPalette.HighlightedText: TEXT,
        QPalette.PlaceholderText: "#8a93a6", QPalette.Link: ACCENT,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, QColor("#9aa4b8"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
