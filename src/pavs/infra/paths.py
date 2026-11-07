"""Where the application keeps its data."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "PolarionValidationStudio"


def data_dir() -> Path:
    """`PAVS_DATA_DIR` if set, else %LOCALAPPDATA%\\PolarionValidationStudio (~/.local/share elsewhere)."""
    override = os.environ.get("PAVS_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    else:
        base = Path.home() / ".local" / "share" / APP_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def database_path() -> Path:
    return data_dir() / "pavs.sqlite3"
