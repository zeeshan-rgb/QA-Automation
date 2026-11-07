# PyInstaller spec: one folder holding the desktop app (PolarionValidationStudio.exe) and the command line tool (pavs-cli.exe).
# Build with tools/build_exe.ps1. Browser checks need Playwright, which is NOT bundled unless PAVS_BUNDLE_PLAYWRIGHT=1.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
src = root / "src"
datas = [
    (str(src / "pavs/polarion/capabilities/data"), "pavs/polarion/capabilities/data"),
    (str(src / "pavs/engines/analysis/data"), "pavs/engines/analysis/data"),
]
binaries, hidden = [], [
    "keyring.backends.Windows", "win32ctypes.pywin32.win32cred", "win32ctypes.pywin32.pywintypes",
    "openpyxl", "docx", "pypdf", "reportlab",
]
excludes = ["tkinter", "mock_polarion", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
            "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"]
if os.environ.get("PAVS_BUNDLE_PLAYWRIGHT") == "1":
    d, b, h = collect_all("playwright")
    datas += d
    binaries += b
    hidden += h
else:
    excludes.append("playwright")

common = dict(pathex=[str(src)], binaries=binaries, datas=datas, hiddenimports=hidden, excludes=excludes, noarchive=False)
gui = Analysis([str(root / "packaging/pavs_gui.py")], **common)
cli = Analysis([str(root / "packaging/pavs_cli.py")], **common)
MERGE((gui, "pavs_gui", "pavs_gui"), (cli, "pavs_cli", "pavs_cli"))

gui_pyz, cli_pyz = PYZ(gui.pure), PYZ(cli.pure)
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name="PolarionValidationStudio", console=False)
cli_exe = EXE(cli_pyz, cli.scripts, [], exclude_binaries=True, name="pavs-cli", console=True)
COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name="PolarionValidationStudio")
