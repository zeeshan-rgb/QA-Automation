"""Read-only reader for a Polarion repository that lives on THIS computer (a local Polarion install).

It runs `svnlook` (bundled with Polarion) against the repository folder, so no repository login is needed. It can only
read: svnlook has no commands that change a repository. Same interface as SvnConfigReader, so the inspection,
script analysis and merge checks use it unchanged.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from ...domain.errors import NotFound, PavsError

MAX_FILE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 30
CANDIDATE_REPOS = ("data/svn/repo", "polarion/data/svn/repo", "Polarion/data/svn/repo")


def find_svnlook(repo_path: str | Path) -> str | None:
    """PAVS_SVNLOOK, then the one bundled with Polarion next to the repository (<root>/bundled/svn/bin), then PATH."""
    candidates: list[Path] = []
    if os.environ.get("PAVS_SVNLOOK"):
        candidates.append(Path(os.environ["PAVS_SVNLOOK"]))
    repo = Path(repo_path)
    for parent in list(repo.parents)[:5]:
        candidates.append(parent / "bundled" / "svn" / "bin" / "svnlook.exe")
        candidates.append(parent / "bundled" / "svn" / "bin" / "svnlook")
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("svnlook")


def is_repository(path: str | Path) -> bool:
    p = Path(path)
    return p.is_dir() and (p / "format").is_file() and (p / "db").is_dir()


def find_local_repository() -> str | None:
    """A Polarion repository in the usual place on any local drive (<drive>:/data/svn/repo)."""
    roots = [Path(f"{d}:/") for d in "CDEFGH"] if os.name == "nt" else [Path("/opt/polarion"), Path("/")]
    for root in roots:
        for rel in CANDIDATE_REPOS:
            c = root / rel
            if is_repository(c):
                return str(c)
    return None


class LocalSvnReader:
    def __init__(self, repo_path: str | Path, svnlook: str, http_root: str) -> None:
        self.repo = str(Path(repo_path))
        self.svnlook = svnlook
        self.root = http_root.rstrip("/") + "/repo"      # only used to build the web address TortoiseSVN needs

    def url(self, path: str) -> str:
        return f"{self.root}/{quote(path.lstrip('/'))}"

    def _run(self, *args: str) -> bytes:
        try:
            r = subprocess.run([self.svnlook, *args], capture_output=True, timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PavsError(f"svnlook could not be run: {type(exc).__name__}") from None
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace")
            if "E160013" in err or "does not exist" in err or "not found" in err.lower():
                raise NotFound(404, "Not Found", f"repository path {args[-1] if args else ''}")
            raise PavsError("svnlook: " + err.strip()[:200])
        return r.stdout

    def probe(self) -> str:
        """Youngest revision number (raises when the repository cannot be read)."""
        return self._run("youngest", self.repo).decode().strip()

    def read_text(self, path: str) -> str:
        data = self._run("cat", self.repo, "/" + path.strip("/"))
        if len(data) > MAX_FILE_BYTES:
            raise PavsError(f"{path} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
        return data.decode("utf-8", "replace")

    def exists(self, path: str) -> bool:
        try:
            self._run("cat", self.repo, "/" + path.strip("/"))
            return True
        except NotFound:
            return False
        except PavsError:
            return True                                   # a directory: it exists

    def list_dir(self, path: str) -> list[str]:
        """Entries directly below `path`; directories end with '/'."""
        out = self._run("tree", self.repo, "/" + path.strip("/"), "--non-recursive").decode("utf-8", "replace")
        names = []
        for i, line in enumerate(out.splitlines()):
            if i == 0 or not line.startswith(" "):
                continue                                  # the first line is the folder itself
            names.append(line[1:])
        return names
