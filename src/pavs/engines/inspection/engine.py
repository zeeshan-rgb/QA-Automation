"""Inspection engine: runs the readers for one project and returns a ConfigSnapshot."""
from __future__ import annotations

from typing import Callable

from ...domain.config import ConfigSnapshot, SectionResult, SectionStatus
from ...polarion.connection import Session
from .base import InspectionContext, log
from .readers_documents import DocumentsReader, PagesReader
from .readers_roles import PermissionsReader, RolesReader
from .readers_workflow import WorkflowReader
from .readers_workitems import (EnumerationsReader, FieldRulesReader, FieldsReader, LinkRolesReader,
                                TypesReader)

# Order matters: later readers use what earlier ones put in ctx.shared (types -> fields -> enums ...).
READERS = (TypesReader, LinkRolesReader, FieldsReader, EnumerationsReader, FieldRulesReader,
           WorkflowReader, RolesReader, PermissionsReader, DocumentsReader, PagesReader)
AREAS = [r.area for r in READERS]


class InspectionEngine:
    def __init__(self, readers=READERS) -> None:
        self.readers = [r() for r in readers]

    def inspect(self, session: Session, project_id: str, areas: list[str] | None = None,
                progress: Callable[[str, int], None] | None = None,
                sample_items: bool = True) -> ConfigSnapshot:
        report = progress or (lambda m, p: None)
        ctx = InspectionContext(session=session, project_id=project_id, sample_items=sample_items)
        snap = ConfigSnapshot(environment_id=session.environment.id, project_id=project_id,
                              release=session.profile.version.release, adapter=session.profile.adapter)
        wanted = set(areas or AREAS)
        # dependencies: fields and workflow need types
        if wanted & {"fields", "workflow", "enumerations"}:
            wanted.add("types")
        todo = [r for r in self.readers if r.area in wanted]
        for i, reader in enumerate(todo):
            report(f"Reading {reader.title.lower()}", int(100 * i / max(len(todo), 1)))
            try:
                section = reader.read(ctx)
            except Exception as exc:     # a reader bug must not lose the other sections
                log.exception("reader %s crashed", reader.area)
                section = SectionResult(area=reader.area, title=reader.title, status=SectionStatus.ERROR,
                                        errors=[f"{type(exc).__name__}: {exc}"])
            snap.sections.append(section)
        report("Done", 100)
        return snap
