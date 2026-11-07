"""Capability registry: which Polarion features can be used on a given server, and how.

Status comes from capability detection (live REST spec, live WSDL, probes) first and from
the documented endpoint catalog for the detected release only as a fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ...domain.enums import CapabilityStatus, ProviderKind, ProviderState, Verification
from ...domain.models import CapabilityRecord
from ..version.release import Release
from .catalog import EndpointCatalog, endpoint_key

DATA = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Strategy:
    provider: ProviderKind
    coverage: str                    # full | partial
    requires: tuple[str, ...]
    probe: str | None = None

    @property
    def label(self) -> str:
        return f"{self.provider.value}: " + ", ".join(self.requires)


@dataclass(frozen=True)
class CapabilityDef:
    id: str
    title: str
    area: str
    strategies: tuple[Strategy, ...]
    notes: str = ""
    write: bool = False


@dataclass
class ResolutionContext:
    """What we know about one server when resolving capabilities."""
    release: Release | None
    providers: dict[ProviderKind, ProviderState]
    live_rest: set[tuple[str, str]] | None = None          # from /rest/v1/definition
    soap_ops: dict[str, set[str] | None] = field(default_factory=dict)  # service -> WSDL ops
    svn_enabled: bool = False


@dataclass
class _Outcome:
    usable: bool
    status: CapabilityStatus
    verification: Verification
    detail: str


class CapabilityRegistry:
    def __init__(self, defs: list[CapabilityDef], catalog: EndpointCatalog) -> None:
        self.defs = defs
        self.catalog = catalog
        self._by_id = {d.id: d for d in defs}

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "CapabilityRegistry":
        raw = yaml.safe_load((DATA / "capabilities.yaml").read_text(encoding="utf-8"))
        defs = []
        for c in raw["capabilities"]:
            strategies = tuple(
                Strategy(ProviderKind(s["provider"]), s.get("coverage", "full"),
                         tuple(s.get("requires", [])), s.get("probe"))
                for s in c["strategies"])
            defs.append(CapabilityDef(c["id"], c["title"], c["area"], strategies,
                                      c.get("notes", ""), bool(c.get("write", False))))
        return cls(defs, EndpointCatalog.load())

    def get(self, cap_id: str) -> CapabilityDef:
        return self._by_id[cap_id]

    def areas(self) -> list[str]:
        return sorted({d.area for d in self.defs})

    # ------------------------------------------------------------------ resolution
    def resolve(self, ctx: ResolutionContext) -> list[CapabilityRecord]:
        return [self.resolve_one(d, ctx) for d in self.defs]

    def resolve_one(self, cap: CapabilityDef, ctx: ResolutionContext) -> CapabilityRecord:
        reasons: list[str] = []
        statuses: list[CapabilityStatus] = []
        best_verification = Verification.DOCUMENTED
        for strat in cap.strategies:
            out = self._evaluate(strat, ctx)
            if out.usable:
                return CapabilityRecord(
                    capability_id=cap.id, title=cap.title, area=cap.area, status=out.status,
                    verification=out.verification, provider=strat.provider,
                    strategy=strat.label, detail=out.detail or cap.notes)
            statuses.append(out.status)
            reasons.append(f"{strat.provider.value}: {out.detail}")
            if out.verification.rank > best_verification.rank:
                best_verification = out.verification
        if CapabilityStatus.NEEDS_PERMISSION in statuses:
            status = CapabilityStatus.NEEDS_PERMISSION
        elif statuses and all(s == CapabilityStatus.UNKNOWN for s in statuses):
            status = CapabilityStatus.UNKNOWN
        else:
            status = CapabilityStatus.UNSUPPORTED
        detail = " | ".join(reasons)
        if cap.notes:
            detail = f"{detail} | {cap.notes}"
        return CapabilityRecord(capability_id=cap.id, title=cap.title, area=cap.area, status=status,
                                verification=best_verification, provider=None,
                                strategy="", detail=detail)

    def _evaluate(self, strat: Strategy, ctx: ResolutionContext) -> _Outcome:
        state = ctx.providers.get(strat.provider, ProviderState.NOT_CONFIGURED)
        if strat.provider == ProviderKind.SVN and not ctx.svn_enabled:
            return _Outcome(False, CapabilityStatus.UNSUPPORTED, Verification.DOCUMENTED,
                            "SVN configuration reader not enabled for this environment")
        if state in (ProviderState.NOT_CONFIGURED,):
            return _Outcome(False, CapabilityStatus.UNSUPPORTED, Verification.DOCUMENTED,
                            "provider not enabled for this environment")
        if state in (ProviderState.UNREACHABLE, ProviderState.DISABLED):
            return _Outcome(False, CapabilityStatus.UNSUPPORTED, Verification.DETECTED,
                            f"provider {state.value} on this server")

        exists, verification, detail = self._requirements(strat, ctx)
        if exists is False:
            return _Outcome(False, CapabilityStatus.UNSUPPORTED, verification, detail)
        if exists is None:
            return _Outcome(False, CapabilityStatus.UNKNOWN, verification, detail)

        status = CapabilityStatus.SUPPORTED if strat.coverage == "full" else CapabilityStatus.PARTIAL
        if state in (ProviderState.AVAILABLE, ProviderState.AUTH_FAILED):
            why = "credentials rejected" if state == ProviderState.AUTH_FAILED else "not signed in"
            return _Outcome(False, CapabilityStatus.NEEDS_PERMISSION, verification,
                            f"available but {why}")
        return _Outcome(True, status, verification, detail)

    def _requirements(self, strat: Strategy, ctx: ResolutionContext
                      ) -> tuple[bool | None, Verification, str]:
        if strat.provider == ProviderKind.REST:
            if ctx.live_rest is not None:
                missing = [r for r in strat.requires if endpoint_key(r) not in ctx.live_rest]
                if missing:
                    return False, Verification.DETECTED, "not in the server's REST spec: " + ", ".join(missing)
                return True, Verification.DETECTED, ""
            results = {r: self.catalog.exists(r, ctx.release) for r in strat.requires}
            if any(v is False for v in results.values()):
                miss = [r for r, v in results.items() if v is False]
                rel = ctx.release.code if ctx.release else "?"
                return False, Verification.DOCUMENTED, f"not in release {rel}: " + ", ".join(miss)
            if any(v is None for v in results.values()):
                return None, Verification.DOCUMENTED, "unknown for this release (no live spec)"
            return True, Verification.DOCUMENTED, ""
        if strat.provider == ProviderKind.SOAP:
            missing, known = [], True
            for req in strat.requires:
                service, op = req.split(".", 1)
                ops = ctx.soap_ops.get(service)
                if ops is None:
                    known = False
                elif op not in ops:
                    missing.append(req)
            if missing:
                return False, Verification.DETECTED, "not in the server's WSDL: " + ", ".join(missing)
            return True, (Verification.DETECTED if known else Verification.DOCUMENTED), ""
        if strat.provider == ProviderKind.SVN:
            return True, Verification.DOCUMENTED, "reads " + ", ".join(strat.requires)
        return None, Verification.DOCUMENTED, f"{strat.provider.value} provider arrives in a later phase"
