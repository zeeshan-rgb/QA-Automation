"""Inspection engine building blocks."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ...domain.config import Gap, SectionResult, SectionStatus, Source
from ...domain.enums import CapabilityStatus, ProviderKind
from ...domain.errors import PavsError, PermissionDenied
from ...domain.models import CapabilityRecord
from ...infra.logging_setup import get_logger
from ...polarion.connection import Session
from .repo import RepoConfig

log = get_logger("inspection")

REMEDY_SVN = "Enable the read-only SVN configuration reader for this environment (needs a Polarion login)."
REMEDY_SOAP = "Sign in to SOAP (token or password) for this environment."
REMEDY_REST = "Sign in to REST with a Personal Access Token (REST must be enabled on the server)."


@dataclass
class InspectionContext:
    session: Session
    project_id: str
    progress: Callable[[str], None] = lambda msg: None
    shared: dict[str, Any] = field(default_factory=dict)   # results readers share (e.g. type ids)
    sample_items: bool = True        # allow read-only sampling of Work Items for per-item workflow actions

    def __post_init__(self) -> None:
        self.caps: dict[str, CapabilityRecord] = {c.capability_id: c for c in self.session.profile.capabilities}
        self.repo = RepoConfig(self.session.svn) if self.session.svn is not None else None

    @property
    def rest(self):
        return self.session.rest

    @property
    def soap(self):
        return self.session.soap

    def usable(self, capability_id: str) -> bool:
        rec = self.caps.get(capability_id)
        return rec is not None and rec.status in (CapabilityStatus.SUPPORTED, CapabilityStatus.PARTIAL)

    def provider_for(self, capability_id: str) -> ProviderKind | None:
        rec = self.caps.get(capability_id)
        return rec.provider if rec and self.usable(capability_id) else None

    def rest_has(self, spec: str) -> bool:
        """True when the REST provider is signed in and the endpoint exists on this server."""
        if self.rest is None:
            return False
        from ...polarion.capabilities import CapabilityRegistry
        from ...polarion.version.release import Release
        reg = CapabilityRegistry.load()
        release = Release.try_parse(self.session.profile.version.release)
        live = self.session.extras.get("live_rest")
        if live is not None:
            from ...polarion.capabilities.catalog import endpoint_key
            return endpoint_key(spec) in live
        return bool(reg.catalog.exists(spec, release))


class Collector:
    """Builds one SectionResult; turns failures into recorded errors/gaps instead of exceptions."""

    def __init__(self, area: str, title: str) -> None:
        self.result = SectionResult(area=area, title=title, status=SectionStatus.COMPLETE)
        self._started = time.perf_counter()
        self._parts_ok = 0
        self._parts_missing = 0

    def used(self, provider: ProviderKind) -> None:
        if provider not in self.result.providers:
            self.result.providers.append(provider)

    def gap(self, item: str, reason: str, remedy: str = "") -> None:
        self.result.gaps.append(Gap(item=item, reason=reason, remedy=remedy))
        self._parts_missing += 1

    def ok(self) -> None:
        self._parts_ok += 1

    def note(self, text: str) -> None:
        self.result.notes.append(text)

    def attempt(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run fn; on Polarion/transport errors record them and return None."""
        try:
            return fn()
        except PermissionDenied as exc:
            self.result.errors.append(f"{label}: permission denied ({exc.detail or exc})")
        except PavsError as exc:
            self.result.errors.append(f"{label}: {exc}")
        except Exception as exc:  # parser bugs etc. must not abort the whole inspection
            log.exception("inspection step %s failed", label)
            self.result.errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None

    def finish(self) -> SectionResult:
        r = self.result
        if self._parts_ok == 0 and (self._parts_missing or r.errors):
            r.status = SectionStatus.UNAVAILABLE if not r.errors else SectionStatus.ERROR
        elif self._parts_missing or r.errors:
            r.status = SectionStatus.PARTIAL
        r.duration_ms = round((time.perf_counter() - self._started) * 1000, 1)
        return r


def first_text(value: Any) -> str:
    """Leaf text of a parsed SOAP value (handles {'_text': ...} and single-key dicts)."""
    while isinstance(value, dict):
        if "_text" in value:
            return value["_text"]
        if len(value) != 1:
            return ""
        value = next(iter(value.values()))
    if isinstance(value, list):
        return first_text(value[0]) if value else ""
    return value or ""


def src_rest(path: str) -> Source:
    return Source(provider=ProviderKind.REST, location=f"GET {path}")


def src_soap(op: str) -> Source:
    return Source(provider=ProviderKind.SOAP, location=op)
