"""Configuration files in the Polarion SVN repository, with project path resolution.

Layout (verified on 2606): global config under `/.polarion/...`; each project under
`<project path>/.polarion/...`, where the path comes from `/.polarion/projects.xml`
(e.g. drivepilot -> /Demo Projects/drivepilot).
"""
from __future__ import annotations

import re

from ...domain.config import Scope, Source
from ...domain.enums import ProviderKind
from ...domain.errors import NotFound, PavsError
from ...polarion.svn.client import SvnConfigReader
from .parsers import parse_projects

GLOBAL_ROOT = ".polarion"


class RepoConfig:
    def __init__(self, svn: SvnConfigReader) -> None:
        self.svn = svn
        self._projects: dict[str, str] | None = None
        self._text: dict[str, str | None] = {}
        self._dirs: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ paths
    def project_root(self, project_id: str) -> str | None:
        if self._projects is None:
            text = self.read(f"{GLOBAL_ROOT}/projects.xml")
            self._projects = parse_projects(text) if text else {}
        path = self._projects.get(project_id)
        return f"{path.strip('/')}/{GLOBAL_ROOT}" if path else None

    def base(self, scope: Scope, project_id: str | None) -> str | None:
        if scope == Scope.GLOBAL:
            return GLOBAL_ROOT
        return self.project_root(project_id) if project_id else None

    # ------------------------------------------------------------------ access
    def read(self, path: str) -> str | None:
        """File text, or None when the file does not exist."""
        path = path.strip("/")
        if path not in self._text:
            try:
                self._text[path] = self.svn.read_text(path)
            except NotFound:
                self._text[path] = None
        return self._text[path]

    def list(self, path: str) -> list[str]:
        """Entry names directly below a directory ('' list when missing). Dirs end with '/'."""
        path = path.strip("/")
        if path not in self._dirs:
            try:
                self._dirs[path] = self.svn.list_dir(path)
            except NotFound:
                self._dirs[path] = []
            except PavsError:
                self._dirs[path] = self._list_via_html(path)
        return self._dirs[path]

    def _list_via_html(self, path: str) -> list[str]:
        """Fallback when PROPFIND is not allowed: parse Apache's SVN directory index."""
        try:
            html = self.svn.read_text(path + "/")
        except PavsError:
            return []
        return [n for n in re.findall(r'<a href="([^"?/][^"]*)"', html) if n not in ("../",)]

    def files(self, scope: Scope, project_id: str | None, rel_dir: str, suffix: str) -> list[tuple[str, str, Source]]:
        """[(file name, text, source)] for files in <scope base>/<rel_dir> ending with suffix."""
        base = self.base(scope, project_id)
        if base is None:
            return []
        d = f"{base}/{rel_dir}".rstrip("/")
        out = []
        for name in self.list(d):
            if name.endswith("/") or not name.endswith(suffix):
                continue
            text = self.read(f"{d}/{name}")
            if text is not None:
                out.append((name, text, self.source(f"{d}/{name}", scope)))
        return out

    def file(self, scope: Scope, project_id: str | None, rel_path: str) -> tuple[str, Source] | None:
        base = self.base(scope, project_id)
        if base is None:
            return None
        path = f"{base}/{rel_path}"
        text = self.read(path)
        return (text, self.source(path, scope)) if text is not None else None

    @staticmethod
    def source(path: str, scope: Scope) -> Source:
        return Source(provider=ProviderKind.SVN, location="/" + path.strip("/"), scope=scope)
