"""Entry point: `python -m pavs` (or the `pavs` script).

    pavs            open the app; if it is already open, bring that window to the front instead
    pavs --new      open another, independent window on purpose
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    args, qt_args = _parser().parse_known_args(sys.argv[1:] if argv is None else argv)

    from PySide6.QtWidgets import QApplication

    from . import APP_NAME
    from .app import build_context
    from .ui import single_instance
    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("PolarionValidationStudio")
    holder: dict = {}                                        # the window does not exist yet while the server is already listening
    server = None
    if not args.new:                                         # an explicit extra window never takes over the "main" role
        server, primary = single_instance.acquire(single_instance.instance_name(),
                                                  lambda: holder["win"].bring_to_front() if "win" in holder else None)
        if not primary:
            return 0                                         # the open window was brought forward; nothing else to do
    apply_theme(app)
    ctx = build_context()
    win = MainWindow(ctx)
    holder["win"] = win
    win.show()
    code = app.exec()
    del server
    return code


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pavs", description="Polarion ALM Validation Studio")
    p.add_argument("--new", action="store_true", help="open another window even if the app is already open")
    return p


if __name__ == "__main__":
    sys.exit(main())
