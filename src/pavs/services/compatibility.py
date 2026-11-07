"""Documented compatibility matrix (what the catalog says) + adapter information.

"Documented" means: derived from the SDK spec and change log, assuming the provider is on
and signed in. It is NOT a claim that the feature was validated on that release; validation
status comes from VERIFIED records gathered by connecting to real servers.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import CapabilityStatus, ProviderKind, ProviderState
from ..polarion.adapters import AdapterManager
from ..polarion.capabilities import CapabilityRegistry, ResolutionContext
from ..polarion.version.release import Release


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    release: Release
    providers: dict


@dataclass
class MatrixRow:
    capability_id: str
    title: str
    area: str
    cells: dict[str, tuple[CapabilityStatus, str]]   # scenario key -> (status, provider)
    notes: str


class CompatibilityService:
    def __init__(self, registry: CapabilityRegistry, adapters: AdapterManager | None = None) -> None:
        self.registry = registry
        self.adapters = adapters or AdapterManager()

    def scenarios(self) -> list[Scenario]:
        on = {ProviderKind.REST: ProviderState.AUTHENTICATED, ProviderKind.SOAP: ProviderState.AUTHENTICATED}
        out = []
        for rel in self.adapters.known_releases:
            out.append(Scenario(rel.code, rel.code, rel, on))
            if rel.code == "2410":
                out.append(Scenario("2410-rest-off", "2410 (REST off)", rel,
                                    {ProviderKind.REST: ProviderState.DISABLED,
                                     ProviderKind.SOAP: ProviderState.AUTHENTICATED}))
        return out

    def documented_matrix(self) -> tuple[list[Scenario], list[MatrixRow]]:
        """Cell = (status, provider). A capability that only the opt-in SVN reader provides is
        reported as (NEEDS_PERMISSION, 'svn opt-in') rather than unsupported."""
        scenarios = self.scenarios()
        rows: dict[str, MatrixRow] = {}
        for sc in scenarios:
            base = self.registry.resolve(ResolutionContext(release=sc.release, providers=sc.providers))
            with_svn = {r.capability_id: r for r in self.registry.resolve(ResolutionContext(
                release=sc.release, providers={**sc.providers, ProviderKind.SVN: ProviderState.AUTHENTICATED},
                svn_enabled=True))}
            for rec in base:
                row = rows.setdefault(rec.capability_id, MatrixRow(
                    rec.capability_id, rec.title, rec.area, {}, self.registry.get(rec.capability_id).notes))
                cell = (rec.status, rec.provider.value if rec.provider else "")
                alt = with_svn[rec.capability_id]
                if rec.status == CapabilityStatus.UNSUPPORTED and alt.status in (
                        CapabilityStatus.SUPPORTED, CapabilityStatus.PARTIAL):
                    cell = (CapabilityStatus.NEEDS_PERMISSION, "svn opt-in")
                row.cells[sc.key] = cell
        return scenarios, sorted(rows.values(), key=lambda r: (r.area, r.capability_id))

    def adapters_info(self) -> list[dict]:
        return [self.adapters.select(r)[0].describe() for r in self.adapters.known_releases]
