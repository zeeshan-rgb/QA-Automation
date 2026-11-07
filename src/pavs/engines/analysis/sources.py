"""Where scripts and templates come from: a local folder, or the Polarion repository (read-only, via the SVN reader)."""
from __future__ import annotations

import html
import re
from pathlib import Path

from ...domain.config import Scope
from ...domain.errors import PavsError
from ..inspection.repo import RepoConfig
from .analyzer import SourceFile

MAX_FILE_BYTES = 1_000_000
MAX_PAGES = 300
LOCAL_SUFFIXES = {".js": "js", ".vm": "vm"}


def kind_of(name: str) -> str | None:
    n = name.lower()
    if n.endswith((".vm", ".vm.txt")):
        return "vm"
    if n.endswith((".js", ".js.txt")):
        return "js"
    return None


def from_folder(folder: str | Path, recursive: bool = True) -> list[SourceFile]:
    root = Path(folder)
    if not root.is_dir():
        raise PavsError(f"'{root}' is not a folder.")
    out = []
    for p in sorted(root.rglob("*") if recursive else root.glob("*")):
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        kind = kind_of(p.name)
        if kind:
            out.append(SourceFile(str(p.relative_to(root)).replace("\\", "/"), kind,
                                  p.read_text(encoding="utf-8", errors="replace"), "local"))
    return out


def velocity_from_page_xml(xml_text: str) -> list[tuple[str, str]]:
    """Velocity code inside Script blocks of a LiveReport/Rich page: [(label, code)]. Pages store it HTML-escaped."""
    out = []
    for i, m in enumerate(re.finditer(r'<sub id="script"[^>]*>(.*?)</sub>', xml_text, re.S), 1):
        body = re.sub(r"<sub[^>]*>", "", m.group(1))
        code = html.unescape(body).strip()
        if "#" in code or "$" in code:
            out.append((f"script block {i}", code))
    return out


def from_repo(repo: RepoConfig, project_id: str, include_global: bool = True) -> list[SourceFile]:
    """Widget templates, Velocity macro libraries, form extensions and page Script blocks. Read-only."""
    out: list[SourceFile] = []
    scopes = [Scope.PROJECT] + ([Scope.GLOBAL] if include_global else [])
    for scope in scopes:
        base = repo.base(scope, project_id)
        if base is None:
            continue
        tag = "project" if scope == Scope.PROJECT else "global"
        # widget packages: pages/widgets/<id>/*.vm
        for w in [n.rstrip("/") for n in repo.list(f"{base}/pages/widgets") if n.endswith("/")]:
            for name, text, _ in repo.files(scope, project_id, f"pages/widgets/{w}", ".vm"):
                out.append(SourceFile(f"{tag}: widgets/{w}/{name}", "vm", text, "repo"))
        for rel in ("pages/scripts/velocity", "scripts", "testing/configuration"):
            for name, text, _ in repo.files(scope, project_id, rel, ".vm"):
                out.append(SourceFile(f"{tag}: {rel}/{name}", "vm", text, "repo"))
        # pages: pages/spaces/<space>/<page>/page.xml
        pages = 0
        for space in [n.rstrip("/") for n in repo.list(f"{base}/pages/spaces") if n.endswith("/")]:
            for page in [n.rstrip("/") for n in repo.list(f"{base}/pages/spaces/{space}") if n.endswith("/")]:
                if pages >= MAX_PAGES:
                    break
                pages += 1
                text = repo.read(f"{base}/pages/spaces/{space}/{page}/page.xml")
                for label, code in velocity_from_page_xml(text or ""):
                    out.append(SourceFile(f"{tag}: page {space}/{page} ({label})", "vm", code, "repo"))
    return [f for f in out if len(f.text) <= MAX_FILE_BYTES]
