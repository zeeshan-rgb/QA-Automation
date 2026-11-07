"""Read-only reader for Polarion configuration stored in the SVN repository (`/repo`).

Opt-in per environment. Only GET and PROPFIND are ever sent; the transport would refuse
anything else because it would count as a write.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote

import httpx

from ...infra import safexml

from ...domain.errors import AuthenticationFailed, NotFound, PermissionDenied, PolarionApiError
from ..transport import HttpTransport

DAV = "DAV:"


class SvnConfigReader:
    def __init__(self, transport: HttpTransport, server_root: str,
                 username: str | None = None, password: str | None = None,
                 repo_path: str = "/repo") -> None:
        self.transport = transport
        self.root = server_root.rstrip("/") + repo_path
        self._auth = httpx.BasicAuth(username, password) if username and password else None

    def url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.root}/{quote(path)}"

    def probe(self) -> httpx.Response:
        return self.transport.request("GET", self.root + "/", provider="svn", auth=self._auth)

    @staticmethod
    def looks_like_svn(resp: httpx.Response) -> bool:
        realm = resp.headers.get("www-authenticate", "")
        return ("subversion" in realm.lower()) or (resp.status_code == 200 and b"svn" in resp.content[:4000].lower())

    def read_text(self, path: str) -> str:
        resp = self.transport.request("GET", self.url(path), provider="svn", auth=self._auth)
        self._raise(resp, path)
        return resp.text

    def exists(self, path: str) -> bool:
        resp = self.transport.request("GET", self.url(path), provider="svn", auth=self._auth)
        if resp.status_code == 404:
            return False
        self._raise(resp, path)
        return True

    def list_dir(self, path: str) -> list[str]:
        """Names of the entries directly below `path` (PROPFIND depth 1)."""
        body = ('<?xml version="1.0" encoding="utf-8"?><propfind xmlns="DAV:">'
                "<prop><resourcetype/></prop></propfind>")
        target = self.url(path).rstrip("/") + "/"
        resp = self.transport.request("PROPFIND", target, provider="svn", auth=self._auth,
                                      headers={"Depth": "1", "Content-Type": "text/xml"},
                                      content=body)
        self._raise(resp, path)
        root = safexml.fromstring(resp.content)
        names = []
        base = unquote(httpx.URL(target).path)
        for href in root.iter(f"{{{DAV}}}href"):
            p = unquote(href.text or "")
            if p.rstrip("/") == base.rstrip("/"):
                continue
            # keep the trailing "/" that marks a collection (directory)
            names.append(p.rstrip("/").rsplit("/", 1)[-1] + ("/" if p.endswith("/") else ""))
        return names

    @staticmethod
    def _raise(resp: httpx.Response, path: str) -> None:
        if resp.status_code < 400:
            return
        cls = {401: AuthenticationFailed, 403: PermissionDenied, 404: NotFound}.get(
            resp.status_code, PolarionApiError)
        raise cls(resp.status_code, resp.reason_phrase, f"SVN path '{path}'")
