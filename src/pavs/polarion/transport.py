"""HTTP transport shared by every provider (REST, SOAP, SVN).

One choke point for TLS, timeouts, retries, the Write Guard, and evidence recording.
Everything recorded is redacted first.
"""
from __future__ import annotations

import ssl
import time
from collections import deque
from typing import Any, Callable

import httpx

from ..domain.errors import TransportError
from ..domain.models import Exchange, TlsSettings
from ..infra.logging_setup import get_logger
from ..infra.redaction import redact_headers, scrub
from .safety import READ_METHODS, WriteAuthorization, WriteGuard

log = get_logger("transport")

RETRY_STATUSES = frozenset({429, 502, 503, 504})
BODY_EXCERPT = 20_000


def build_ssl_context(tls: TlsSettings) -> ssl.SSLContext | bool:
    """System trust store (Windows certificate store on Windows) plus an optional CA bundle."""
    if not tls.verify:
        return False
    ctx = ssl.create_default_context()
    if tls.ca_bundle:
        try:
            ctx.load_verify_locations(cafile=tls.ca_bundle)
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"Cannot load CA bundle '{tls.ca_bundle}': {exc}") from None
    return ctx


class ExchangeRecorder:
    """Keeps the most recent exchanges (already redacted) for evidence."""

    def __init__(self, limit: int = 500) -> None:
        self._items: deque[Exchange] = deque(maxlen=limit)
        self.listeners: list[Callable[[Exchange], None]] = []

    def add(self, ex: Exchange) -> None:
        self._items.append(ex)
        for cb in list(self.listeners):
            try:
                cb(ex)
            except Exception:  # pragma: no cover - listener bugs must not break requests
                log.exception("exchange listener failed")

    def items(self) -> list[Exchange]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()


def _excerpt(data: bytes | str | None, content_type: str = "") -> str | None:
    if data is None or data == b"" or data == "":
        return None
    if isinstance(data, bytes):
        if content_type and not any(t in content_type for t in ("json", "xml", "text", "html")):
            return f"<{len(data)} bytes of {content_type or 'binary'}>"
        data = data.decode("utf-8", errors="replace")
    if len(data) > BODY_EXCERPT:
        data = data[:BODY_EXCERPT] + f"... <truncated, {len(data)} chars>"
    return scrub(data)


class HttpTransport:
    def __init__(self, tls: TlsSettings, timeout: float, guard: WriteGuard,
                 recorder: ExchangeRecorder | None = None, max_retries: int = 2,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 user_agent: str = "PolarionValidationStudio/0.1") -> None:
        self.guard = guard
        self.recorder = recorder or ExchangeRecorder()
        self.max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(
            verify=build_ssl_context(tls),
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, method: str, url: str, *, provider: str,
                headers: dict[str, str] | None = None, params: Any = None,
                json: Any = None, content: bytes | str | None = None, auth: Any = None,
                write_auth: WriteAuthorization | None = None, is_write: bool | None = None,
                project_id: str | None = None, retry: bool | None = None) -> httpx.Response:
        method = method.upper()
        write = (method not in READ_METHODS) if is_write is None else is_write
        self.guard.check(method, url, write_auth, is_write=write, project_id=project_id)
        may_retry = (not write) if retry is None else retry

        attempt = 0
        while True:
            started = time.perf_counter()
            req = self._client.build_request(method, url, headers=headers, params=params,
                                             json=json, content=content)
            try:
                resp = self._client.send(req, auth=auth)
                resp.read()
            except httpx.TimeoutException as exc:
                self._record(provider, req, None, started, f"timeout: {type(exc).__name__}")
                if may_retry and attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                raise TransportError(f"Timed out talking to {req.url.host}") from None
            except httpx.TransportError as exc:
                msg = self._describe(exc)
                self._record(provider, req, None, started, msg)
                if may_retry and attempt < self.max_retries and isinstance(exc, httpx.ConnectError) \
                        and "certificate" not in msg.lower():
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                raise TransportError(msg) from None
            self._record(provider, req, resp, started, None)
            if may_retry and resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp

    # ------------------------------------------------------------------ helpers
    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), 30.0)
        self._sleep(delay)

    @staticmethod
    def _describe(exc: Exception) -> str:
        text = str(exc) or type(exc).__name__
        low = text.lower()
        if "certificate" in low or "ssl" in low:
            return f"TLS/certificate problem: {text}"
        if isinstance(exc, httpx.ConnectError):
            return f"Cannot connect: {text}"
        return f"{type(exc).__name__}: {text}"

    def _record(self, provider: str, req: httpx.Request, resp: httpx.Response | None,
                started: float, error: str | None) -> None:
        req_ct = req.headers.get("content-type", "")
        body = req.content if req.content else None
        ex = Exchange(
            provider=provider, method=req.method, url=scrub(str(req.url)) or "",
            status=resp.status_code if resp is not None else None,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            request_headers=redact_headers(dict(req.headers)),
            request_body=_excerpt(body, req_ct),
            response_headers=redact_headers(dict(resp.headers)) if resp is not None else {},
            response_body=_excerpt(resp.content, resp.headers.get("content-type", "")) if resp is not None else None,
            error=scrub(error),
        )
        self.recorder.add(ex)
        log.debug("%s %s -> %s (%s ms)", ex.method, ex.url, ex.status or ex.error, ex.duration_ms)
