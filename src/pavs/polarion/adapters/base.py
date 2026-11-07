"""Version adapters hold BEHAVIOUR differences between releases.

Endpoint availability lives in the generated catalog, not here. An adapter only overrides
what really behaves differently, and lists known quirks so reports can explain them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.enums import CapabilityStatus
from ...domain.models import CapabilityRecord
from ..version.release import Release


@dataclass(frozen=True)
class Quirk:
    id: str
    text: str
    capability_ids: tuple[str, ...] = ()
    source: str = ""


@dataclass
class VersionAdapter:
    """Newest known behaviour. Release adapters subclass and override."""
    release: Release | None = None
    name: str = "Base"
    validated: bool = False           # True only after a VERIFIED compatibility run
    quirks: list[Quirk] = field(default_factory=list)

    # Hooks that inspection/testing readers will call in later phases.
    def custom_fields_via_rest(self) -> bool:
        return True

    def save_hooks_scoped_by_type(self) -> bool:
        return True

    def adjust(self, record: CapabilityRecord) -> CapabilityRecord:
        """Annotate a resolved capability with release-specific knowledge."""
        notes = [q.text for q in self.quirks if record.capability_id in q.capability_ids]
        if notes:
            record = record.model_copy(update={"detail": " | ".join(filter(None, [record.detail] + notes))})
        return record

    def warnings(self) -> list[str]:
        if self.validated:
            return []
        label = self.release.code if self.release else "this release"
        return [f"Adapter '{self.name}' has not yet been validated against a real Polarion {label}."]

    def describe(self) -> dict:
        return {"name": self.name, "release": self.release.code if self.release else None,
                "validated": self.validated, "quirks": [q.text for q in self.quirks]}


def downgrade(record: CapabilityRecord, status: CapabilityStatus, why: str) -> CapabilityRecord:
    return record.model_copy(update={"status": status, "detail": f"{record.detail} | {why}".strip(" |")})
