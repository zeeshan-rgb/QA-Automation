"""TortoiseSVN hand-off: open its repository browser, log, blame, compare and merge dialogs.

This application never commits, merges or checks out anything itself. It prepares and checks the work (read-only),
then starts the TortoiseSVN dialog so the person finishes it with their own SVN login and sees every change before
it is committed. No password is ever put on a command line: TortoiseSVN uses its own cached sign-in.

Documented command line: https://tortoisesvn.net/docs/release/TortoiseSVN_en/tsvn-automation.html
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from ..domain.errors import ConfigurationError

_FORBIDDEN = set('"\r\n\t\x00<>|')


@dataclass(frozen=True)
class TortoiseInfo:
    proc: Path                  # TortoiseProc.exe
    merge: Path | None          # TortoiseMerge.exe (compares two local files)

    @property
    def folder(self) -> Path:
        return self.proc.parent


def find_tortoise() -> TortoiseInfo | None:
    """Registry first (what the installer writes), then the default folders. PAVS_TORTOISE_DIR overrides both."""
    candidates: list[Path] = []
    override = os.environ.get("PAVS_TORTOISE_DIR")
    if override:
        candidates.append(Path(override))
    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\TortoiseSVN") as key:
                        proc, _ = winreg.QueryValueEx(key, "ProcPath")
                        candidates.append(Path(proc).parent)
                except OSError:
                    continue
        except ImportError:  # pragma: no cover
            pass
        for env in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "TortoiseSVN" / "bin")
    for folder in candidates:
        proc = folder / "TortoiseProc.exe"
        if proc.is_file():
            merge = folder / "TortoiseMerge.exe"
            return TortoiseInfo(proc, merge if merge.is_file() else None)
    return None


def clean_url(url: str) -> str:
    """An http(s) or svn URL with spaces and unsafe characters percent-encoded; anything odd is refused."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "svn", "svn+ssh", "file") or not parsed.netloc and parsed.scheme != "file":
        raise ConfigurationError(f"'{url}' is not a repository URL.")
    if any(ch in _FORBIDDEN for ch in url):
        raise ConfigurationError("The repository URL contains characters that cannot be passed to TortoiseSVN safely.")
    return quote(url, safe="/:%@?=&+~!$'()*,;#[]")


def clean_path(path: str | Path, must_exist: bool = True, folder: bool | None = None) -> str:
    text = str(path or "").strip()
    if not text or any(ch in _FORBIDDEN for ch in text):
        raise ConfigurationError("The folder or file name contains characters that cannot be passed to TortoiseSVN safely.")
    p = Path(text)
    if must_exist and not p.exists():
        raise ConfigurationError(f"'{text}' does not exist.")
    if folder is True and p.exists() and not p.is_dir():
        raise ConfigurationError(f"'{text}' is not a folder.")
    return str(p)


Spawn = Callable[[str], None]


def _spawn(command_line: str) -> None:  # pragma: no cover - starts a real desktop program
    subprocess.Popen(command_line, shell=False, close_fds=True)


class TortoiseLauncher:
    """Builds a TortoiseProc command line and starts it. `spawn` is replaced in tests so nothing opens on the desktop."""

    def __init__(self, info: TortoiseInfo | None = None, spawn: Spawn | None = None) -> None:
        self.info = info if info is not None else find_tortoise()
        self._spawn = spawn or _spawn

    @property
    def available(self) -> bool:
        return self.info is not None

    def _need(self) -> TortoiseInfo:
        if self.info is None:
            raise ConfigurationError("TortoiseSVN was not found. Install it from https://tortoisesvn.net "
                                     "(or set PAVS_TORTOISE_DIR to its bin folder).")
        return self.info

    def _proc(self, command: str, **params: str) -> str:
        info = self._need()
        parts = [f'"{info.proc}"', f"/command:{command}"]
        parts += [f'/{key}:"{value}"' for key, value in params.items() if value != ""]
        line = " ".join(parts)
        self._spawn(line)
        return line

    # ---------------------------------------------------------------- looking (nothing is changed)
    def repo_browser(self, url: str, revision: str = "") -> str:
        return self._proc("repobrowser", path=clean_url(url), rev=revision)

    def log(self, url_or_path: str) -> str:
        return self._proc("log", path=self._target(url_or_path))

    def blame(self, url_or_path: str) -> str:
        return self._proc("blame", path=self._target(url_or_path))

    def compare_urls(self, url1: str, url2: str) -> str:
        """The 'compare URLs' dialog: lists every changed file, double-click opens its diff."""
        return self._proc("showcompare", url1=clean_url(url1), url2=clean_url(url2), revision1="HEAD", revision2="HEAD")

    def diff_urls(self, url1: str, url2: str) -> str:
        return self._proc("diff", path=clean_url(url1), path2=clean_url(url2))

    def diff_local(self, file1: str | Path, file2: str | Path) -> str:
        """Two local files side by side in TortoiseMerge."""
        info = self._need()
        if info.merge is None:
            raise ConfigurationError("TortoiseMerge.exe was not found next to TortoiseProc.exe.")
        a, b = clean_path(file1), clean_path(file2)
        line = f'"{info.merge}" /base:"{a}" /mine:"{b}"'
        self._spawn(line)
        return line

    # ---------------------------------------------------------------- hand-offs that can change a repository
    def checkout(self, url: str, folder: str | Path) -> str:
        return self._proc("checkout", url=clean_url(url), path=clean_path(folder, must_exist=True, folder=True))

    def update(self, working_copy: str | Path) -> str:
        return self._proc("update", path=clean_path(working_copy, folder=True))

    def merge(self, working_copy: str | Path) -> str:
        """The merge wizard on a working copy: the real 3-way merge with history and conflict resolution."""
        return self._proc("merge", path=clean_path(working_copy, folder=True))

    def conflict_editor(self, file: str | Path) -> str:
        return self._proc("conflicteditor", path=clean_path(file))

    def commit(self, working_copy: str | Path) -> str:
        return self._proc("commit", path=clean_path(working_copy))

    @staticmethod
    def _target(value: str) -> str:
        return clean_url(value) if "://" in value else clean_path(value)
