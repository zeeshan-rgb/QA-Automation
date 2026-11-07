"""The four engines (Phases 2-6). Interfaces are fixed now so later phases plug in.

- inspection : reads Polarion configuration through providers chosen by the capability registry
- testing    : executes checks (all writes go through the Write Guard)
- validation : expected vs actual -> verdicts + evidence
"""
from __future__ import annotations

from typing import Protocol

from ..domain.models import CapabilityRecord


class InspectionReader(Protocol):
    """One reader per configuration area (roles, fields, workflows, ...)."""
    capability_id: str

    def available(self, capabilities: list[CapabilityRecord]) -> bool: ...


class Check(Protocol):
    """One executable validation step (Phase 4)."""
    id: str
    requirement_id: str
    writes: bool

    def run(self, session) -> "object": ...
