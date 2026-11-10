"""Polarion REST API v1 client (JSON:API style)."""
from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import httpx

from ...domain.errors import (AuthenticationFailed, NotFound, PermissionDenied,
                              PolarionApiError)
from ..safety import WriteAuthorization
from ..transport import HttpTransport

JSON = "application/json"


def parse_errors(resp: httpx.Response) -> tuple[str, str, str]:
    """(title, detail, source) of the first JSON:API error, or the HTTP reason."""
    try:
        body = resp.json()
        err = (body.get("errors") or [{}])[0]
        src = err.get("source") or {}
        source = src.get("parameter") or src.get("pointer") or "" if isinstance(src, dict) else ""
        return err.get("title", resp.reason_phrase), err.get("detail", ""), source
    except Exception:
        return resp.reason_phrase, "", ""


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    title, detail, source = parse_errors(resp)
    cls = {401: AuthenticationFailed, 403: PermissionDenied, 404: NotFound}.get(
        resp.status_code, PolarionApiError)
    raise cls(resp.status_code, title, detail, source)


def is_jsonapi_response(resp: httpx.Response) -> bool:
    return "json" in resp.headers.get("content-type", "")


class RestClient:
    def __init__(self, transport: HttpTransport, base_url: str, token: str | None = None) -> None:
        self.transport = transport
        self.root = base_url.rstrip("/") + "/rest/v1"
        self._token = token

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.root + ("" if path.startswith("/") else "/") + path

    def _headers(self, extra: dict[str, str] | None = None, body: bool = False) -> dict[str, str]:
        h = {"Accept": JSON}
        if body:
            h["Content-Type"] = JSON
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------------ raw access
    def send(self, method: str, path: str, *, params: Any = None, json: Any = None,
             write_auth: WriteAuthorization | None = None, headers: dict[str, str] | None = None,
             authenticated: bool = True) -> httpx.Response:
        hdrs = self._headers(headers, body=json is not None)
        if not authenticated:
            hdrs.pop("Authorization", None)
        return self.transport.request(method, self.url(path), provider="rest", headers=hdrs,
                                      params=params, json=json, write_auth=write_auth)

    # ------------------------------------------------------------------ convenience
    def get(self, path: str, params: Any = None) -> dict:
        resp = self.send("GET", path, params=params)
        raise_for_status(resp)
        return resp.json() if resp.content else {}

    def iterate(self, path: str, params: dict | None = None, page_size: int = 100,
                max_items: int | None = None) -> Iterator[dict]:
        """Yield every resource of a list endpoint, following `links.next`."""
        query = dict(params or {})
        query.setdefault("page[size]", page_size)
        query.setdefault("page[number]", 1)
        url: str | None = self.url(path)
        first = True
        seen = 0
        while url:
            resp = self.send("GET", url, params=query if first else None)
            raise_for_status(resp)
            body = resp.json()
            for item in body.get("data") or []:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            nxt = (body.get("links") or {}).get("next")
            if nxt:
                target = urljoin(self.url(path), nxt)
                if self._same_origin(target):
                    url = target
                else:
                    # A server (or proxy) naming ANOTHER host in links.next must never receive our bearer token: ask the
                    # configured server for the next page number instead of following the foreign address.
                    query["page[number]"] = int(query.get("page[number]", 1)) + 1
                    url = self.url(path)
                    first = True
                    continue
            else:
                url = None
            first = False

    def _same_origin(self, url: str) -> bool:
        a, b = urlparse(url), urlparse(self.root)
        return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())

    def probe(self, path: str, params: Any = None) -> httpx.Response:
        """GET without raising: used for capability probes."""
        return self.send("GET", path, params=params)

    def current_user(self) -> dict:
        return self.get("/user", params={"fields[users]": "id,name"})

    def metadata(self) -> dict:
        return self.get("/metadata")
