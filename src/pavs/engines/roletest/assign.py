"""Assign roles to a user for a role test, and put the original assignment back afterwards.

Polarion's SOAP API cannot assign roles; they live in <project>/.polarion/security/user-roles.xml in the repository. This
module changes ONLY that one file, only the entry of the one test user, through a throw-away working copy and the Subversion
command-line client. The exact original text is kept so it can be restored byte for byte. Nothing else in the repository is touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

from ...domain.errors import PavsError
from ...infra import safexml
from ...infra.redaction import scrub

ROLES_FILE = "user-roles.xml"
TIMEOUT_SECONDS = 120


def find_svn(hint_dir: str | Path | None = None) -> str | None:
    """PAVS_SVN, then the client bundled with Polarion (next to svnlook), then TortoiseSVN's, then PATH."""
    cands: list[Path] = []
    if os.environ.get("PAVS_SVN"):
        cands.append(Path(os.environ["PAVS_SVN"]))
    if hint_dir:
        for parent in [Path(hint_dir), *list(Path(hint_dir).parents)[:5]]:
            cands += [parent / "bundled" / "svn" / "bin" / "svn.exe", parent / "bundled" / "svn" / "bin" / "svn"]
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cands.append(Path(root) / "TortoiseSVN" / "bin" / "svn.exe")
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("svn")


def set_user_roles(text: str | None, user: str, roles: list[str]) -> str:
    """user-roles.xml with `user`'s entry replaced by exactly `roles` (the others are left as they are)."""
    if text and text.strip():
        try:
            root = safexml.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise PavsError(f"{ROLES_FILE} could not be parsed ({exc}); nothing was changed.") from None
    else:
        root = ET.Element("user-roles")
    for e in [e for e in root.findall("user") if e.get("name") == user]:
        root.remove(e)
    if roles:
        entry = ET.SubElement(root, "user", {"name": user})
        for r in roles:
            ET.SubElement(entry, "role", {"name": r})
    ET.indent(root, space="    ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def roles_in(text: str | None, user: str) -> list[str]:
    if not text or not text.strip():
        return []
    try:
        root = safexml.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return []
    return [r.get("name", "") for e in root.findall("user") if e.get("name") == user for r in e.findall("role")]


@dataclass
class Backup:
    """What was there before. Stored so a restore is possible even after the app was closed."""
    user: str
    folder_url: str              # URL of <project>/.polarion/security
    original: str | None         # exact text of user-roles.xml; None = the file did not exist
    username: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Backup":
        return Backup(d["user"], d["folder_url"], d.get("original"), d.get("username", ""))


class RoleAssigner:
    def __init__(self, svn_exe: str, folder_url: str, username: str, password: str | None = None, run=subprocess.run) -> None:
        self.svn, self.folder_url, self.username, self.password, self._run = svn_exe, folder_url, username, password, run

    # ------------------------------------------------------------------ svn plumbing
    def _svn(self, *args: str, cwd: str | None = None) -> str:
        base = [self.svn, *args, "--non-interactive", "--no-auth-cache"]
        stdin = None
        if self.username:
            base += ["--username", self.username]
        if self.password:
            base += ["--password-from-stdin"]
            stdin = self.password + "\n"
        try:
            r = self._run(base, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PavsError(f"Subversion could not be run: {type(exc).__name__}") from None
        if r.returncode != 0:
            err = scrub(((r.stderr or "") + (r.stdout or "")).strip()) or "unknown error"
            if self.password:
                err = err.replace(self.password, "***")
            raise PavsError("Subversion: " + err[-300:])
        return r.stdout or ""

    def _checkout(self, tmp: str) -> Path:
        self._svn("checkout", "--depth", "files", self.folder_url, tmp)
        return Path(tmp) / ROLES_FILE

    # ------------------------------------------------------------------ public
    def assign(self, user: str, roles: list[str]) -> Backup:
        """Give `user` exactly `roles` in the project. Returns what to restore."""
        with tempfile.TemporaryDirectory(prefix="pavs-roles-") as tmp:
            f = self._checkout(tmp)
            original = f.read_bytes().decode("utf-8") if f.is_file() else None
            backup = Backup(user, self.folder_url, original, self.username)
            new = set_user_roles(original, user, roles)
            if original is not None and new.replace("\r", "") == original.replace("\r", ""):
                return backup                                              # already exactly that: nothing to commit
            existed = f.is_file()
            f.write_bytes(new.encode("utf-8"))
            if not existed:
                self._svn("add", ROLES_FILE, cwd=tmp)
            self._svn("commit", "-m", f"PAVS role test: {user} = {', '.join(roles) or '(none)'} (temporary, restored after the test)", cwd=tmp)
        return backup

    def restore(self, backup: Backup) -> bool:
        """Put the original file back. True when something was committed."""
        with tempfile.TemporaryDirectory(prefix="pavs-roles-") as tmp:
            f = self._checkout(tmp)
            now = f.read_bytes().decode("utf-8") if f.is_file() else None
            if backup.original is None:
                if now is None:
                    return False
                self._svn("delete", ROLES_FILE, cwd=tmp)
            else:
                if now is not None and now.replace("\r", "") == backup.original.replace("\r", ""):
                    return False
                f.write_bytes(backup.original.encode("utf-8"))
            self._svn("commit", "-m", f"PAVS role test: restore the roles of {backup.user}", cwd=tmp)
        return True
