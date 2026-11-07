"""Polarion version detection from several independent signals.

Unauthenticated signals (verified on 2606):
- login page `buildId=YYYYMMDD-HHMM-<release>.<build>-<hash>`  (0.9)
- login page text "Version NNNN"                                (0.8)
- /polarion/sdk/doc/rest/changes.txt first "Version 3.YY.M"     (0.7)
- live REST spec /polarion/rest/v1/definition -> minimum release (0.5)
Authenticated:
- REST GET /metadata (2512+) data.attributes.version / build     (1.0)
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable

import httpx

from ...domain.errors import PavsError
from ...domain.models import VersionInfo, VersionSignal
from ...infra.logging_setup import get_logger
from ..capabilities.catalog import EndpointCatalog, norm_path
from ..transport import HttpTransport
from .release import Release

log = get_logger("version")

_BUILD_ID = re.compile(r"buildId=(\d{8}-\d{4}-(\d{4})\.(\d+)-[0-9a-f]+)", re.I)
_VERSION_TEXT = re.compile(r"\bVersion\s+(\d{4})\b")
_CHANGES = re.compile(r"^\s*Version\s+(3\.\d{2}\.\d{1,2})\s*$", re.M)
_META_BUILD_RELEASE = re.compile(r"-(\d{4})\.\d+-")


class VersionDetector:
    def __init__(self, transport: HttpTransport, base_url: str,
                 catalog: EndpointCatalog | None = None) -> None:
        self.transport = transport
        self.base = base_url.rstrip("/")
        self.catalog = catalog or EndpointCatalog.load()
        self.live_rest: set[tuple[str, str]] | None = None   # filled by spec probe

    # ------------------------------------------------------------------ public
    def detect(self, rest_metadata: Callable[[], dict] | None = None) -> VersionInfo:
        signals: list[VersionSignal] = []
        build: str | None = None
        for probe in (self._login_page, self._sdk_changes, self._live_spec):
            try:
                found = probe()
            except PavsError as exc:
                log.info("version probe %s failed: %s", probe.__name__, exc)
                continue
            for sig, b in found:
                signals.append(sig)
                build = build or b
        if rest_metadata is not None:
            try:
                for sig, b in self.metadata_signals(rest_metadata):
                    signals.append(sig)
                    build = b or build
            except PavsError as exc:
                log.info("metadata probe failed: %s", exc)
        return self.combine(signals, build)

    @staticmethod
    def combine(signals: list[VersionSignal], build: str | None = None) -> VersionInfo:
        info = VersionInfo(signals=signals, build=build)
        # Fingerprint signals give a lower bound, not an exact release: count them separately.
        exact = [s for s in signals if s.release and not s.source.startswith("rest-spec")]
        scores: dict[str, float] = defaultdict(float)
        for s in exact:
            scores[s.release] += s.confidence  # type: ignore[index]
        if scores:
            best = max(scores, key=lambda r: (scores[r], r))
            info.release = best
            info.confidence = round(min(1.0, max(s.confidence for s in exact if s.release == best)), 2)
            others = sorted(r for r in scores if r != best)
            if others:
                info.warnings.append(
                    f"Version signals disagree ({', '.join([best] + others)}); using {best}.")
        floor = [s for s in signals if s.source.startswith("rest-spec") and s.release]
        if floor:
            low = max(Release.parse(s.release) for s in floor)  # type: ignore[arg-type]
            if info.release is None:
                info.release = low.code
                info.confidence = 0.5
                info.warnings.append(f"Only the REST endpoint fingerprint was found: release {low.code} or newer.")
            elif Release.parse(info.release) < low:
                info.warnings.append(
                    f"The server's REST spec contains endpoints from {low.code}, newer than the "
                    f"detected {info.release}. Trusting the endpoint list for capabilities.")
        if info.release:
            info.internal_version = Release.parse(info.release).internal
        else:
            info.warnings.append("Polarion version could not be detected.")
        return info

    # ------------------------------------------------------------------ probes
    def _get(self, url: str) -> httpx.Response:
        return self.transport.request("GET", url, provider="detect")

    def _login_page(self) -> list[tuple[VersionSignal, str | None]]:
        resp = self._get(self.base + "/")
        if resp.status_code != 200:
            return []
        text = resp.text
        out: list[tuple[VersionSignal, str | None]] = []
        m = _BUILD_ID.search(text)
        if m:
            out.append((VersionSignal(source="login-page buildId", value=m.group(1),
                                      release=m.group(2), confidence=0.9), m.group(1)))
        m = _VERSION_TEXT.search(text)
        if m and Release.try_parse(m.group(1)):
            out.append((VersionSignal(source="login-page text", value=m.group(0),
                                      release=m.group(1), confidence=0.8), None))
        return out

    def _sdk_changes(self) -> list[tuple[VersionSignal, str | None]]:
        resp = self._get(self.base + "/sdk/doc/rest/changes.txt")
        if resp.status_code != 200 or "text" not in resp.headers.get("content-type", "text"):
            return []
        m = _CHANGES.search(resp.text[:4000])
        if not m:
            return []
        rel = Release.parse(m.group(1))
        return [(VersionSignal(source="sdk rest changes.txt", value=m.group(1),
                               release=rel.code, confidence=0.7), None)]

    def _live_spec(self) -> list[tuple[VersionSignal, str | None]]:
        resp = self._get(self.base + "/rest/v1/definition")
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            return []
        try:
            spec = resp.json()
        except ValueError:
            return []
        present = set()
        for path, item in (spec.get("paths") or {}).items():
            for method in item:
                if method.lower() in ("get", "post", "patch", "delete", "put"):
                    present.add((method.upper(), norm_path(path)))
        if not present:
            return []
        self.live_rest = present
        low = self.catalog.min_release_for(present)
        if low is None:
            return []
        return [(VersionSignal(source="rest-spec fingerprint", value=f"{len(present)} operations",
                               release=low.code, confidence=0.5), None)]

    def metadata_signals(self, fetch: Callable[[], dict]) -> list[tuple[VersionSignal, str | None]]:
        """Signal from REST GET /metadata (2512+); `fetch` returns the JSON body."""
        body = fetch()
        attrs = ((body or {}).get("data") or {}).get("attributes") or {}
        version, build = attrs.get("version"), attrs.get("build")
        rel = Release.try_parse(version) if version else None
        if rel is None and build:
            m = _META_BUILD_RELEASE.search(build)
            rel = Release.try_parse(m.group(1)) if m else None
        if rel is None:
            return []
        return [(VersionSignal(source="rest /metadata", value=str(version or build),
                               release=rel.code, confidence=1.0), build)]
