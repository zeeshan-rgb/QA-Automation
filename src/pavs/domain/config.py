"""Models for inspected Polarion configuration (Phase 2).

Every item carries `sources`: where it was read from (provider, scope, file/endpoint).
APIs (REST/SOAP) give the EFFECTIVE configuration; the SVN files give the LAYERS
(global vs project, generic vs type-specific) that explain it.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .enums import ProviderKind
from .models import utcnow


class Scope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"
    EFFECTIVE = "effective"       # as returned by the server API (already merged)


class Source(BaseModel):
    provider: ProviderKind
    location: str                 # svn path, REST path or SOAP operation
    scope: Scope = Scope.EFFECTIVE

    def label(self) -> str:
        return f"{self.provider.value.upper()} {self.scope.value}: {self.location}"


class SectionStatus(str, Enum):
    COMPLETE = "complete"         # everything this section covers was read
    PARTIAL = "partial"           # some parts read, some gaps
    UNAVAILABLE = "unavailable"   # nothing could be read (provider off, no permission, unsupported)
    ERROR = "error"


class Gap(BaseModel):
    """Something that could not be inspected, and what would make it possible."""
    item: str
    reason: str
    remedy: str = ""


# ---------------------------------------------------------------- roles & permissions
class RoleDef(BaseModel):
    id: str
    scope: Scope
    sources: list[Source] = Field(default_factory=list)


class RoleAssignment(BaseModel):
    principal: str                # user id (or group id when known)
    principal_kind: str = "user"
    role: str
    scope: Scope
    sources: list[Source] = Field(default_factory=list)


class CustomSet(BaseModel):
    id: str
    title: str = ""
    query: str = ""
    scope: Scope


class PermissionRule(BaseModel):
    role: str
    permission: str               # fully qualified, e.g. com.polarion.persistence.object.WorkItem.modify
    effect: str                   # grant | deny
    scope: Scope
    sources: list[Source] = Field(default_factory=list)

    @property
    def customsets(self) -> list[str]:
        import re
        return re.findall(r"@([A-Za-z0-9_\-]+)", self.permission)


class PermissionCheck(BaseModel):
    user: str
    permission: str
    project: str | None
    granted: bool | None
    detail: str = ""
    source: Source | None = None


# ---------------------------------------------------------------- work items, fields, enums
class EnumOptionDef(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    default: bool = False
    hidden: bool = False
    sort_order: int | None = None
    icon: str = ""
    color: str = ""
    opposite_name: str = ""       # link roles
    parent: bool = False          # link roles: parent-child semantics
    terminal: bool = False        # statuses
    rules: list[tuple[str, str]] = Field(default_factory=list)   # link role rules (from, to)
    extra: dict[str, Any] = Field(default_factory=dict)


class EnumerationDef(BaseModel):
    enum_id: str                  # e.g. "status", "workitem-type", "documents/document-status"
    target_type: str | None = None   # Work Item type for type-specific enums
    scope: Scope
    options: list[EnumOptionDef] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.enum_id}@{self.target_type or '*'}"


class FieldDef(BaseModel):
    id: str
    name: str = ""
    type: str = ""                # Polarion type string: string, text/html, enum:priority, enum:@user, ...
    enum_id: str | None = None
    required: bool = False
    multi: bool = False
    default: str | None = None
    description: str = ""
    depends_on: str | None = None
    target_type: str | None = None   # None = all Work Item types
    scope: Scope
    sources: list[Source] = Field(default_factory=list)


class FieldRule(BaseModel):
    """UI-only rules: read-only fields per status, form filters per action/state."""
    kind: str                     # read_only | form_filter_action | form_filter_state
    view: str = "_default"        # interface view ("hat") the rule belongs to
    target_type: str | None
    ref: str                      # status id or action id ("_default", "_create" are special)
    fields: list[str]
    excluded: list[str] = Field(default_factory=list)
    scope: Scope
    sources: list[Source] = Field(default_factory=list)


# ---------------------------------------------------------------- workflow
class RuleCall(BaseModel):
    """A workflow condition or function with its parameters."""
    name: str
    params: dict[str, str] = Field(default_factory=dict)


class WorkflowActionDef(BaseModel):
    id: str
    name: str = ""
    required_fields: list[str] = Field(default_factory=list)
    cleared_fields: list[str] = Field(default_factory=list)
    required_roles: list[str] = Field(default_factory=list)
    conditions: list[RuleCall] = Field(default_factory=list)
    functions: list[RuleCall] = Field(default_factory=list)
    signature_policy: str | None = None
    auto_signature: bool = False


class Transition(BaseModel):
    action: str
    source: str = Field(alias="from")
    target: str = Field(alias="to")

    model_config = {"populate_by_name": True}


class WorkflowDef(BaseModel):
    prototype: str                # WorkItem | Module | TestRun
    target_type: str | None       # Work Item / Document type, None = generic
    scope: Scope
    initial_status: str | None = None
    initial_action: str | None = None
    actions: list[WorkflowActionDef] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @property
    def statuses(self) -> list[str]:
        seen: list[str] = []
        for s in [self.initial_status] + [t.source for t in self.transitions] + [t.target for t in self.transitions]:
            if s and s not in seen:
                seen.append(s)
        return seen

    def action(self, action_id: str) -> WorkflowActionDef | None:
        return next((a for a in self.actions if a.id == action_id), None)


class ItemWorkflowAction(BaseModel):
    """Per-Work-Item view from the server (REST getWorkflowActions / SOAP getAvailableActions)."""
    work_item: str = ""
    work_item_type: str = ""
    current_status: str = ""
    action_id: str
    name: str = ""
    available: bool
    target_status: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    required_roles: list[str] = Field(default_factory=list)
    signature_required: bool = False
    unavailable_reason: str = ""


# ---------------------------------------------------------------- documents & pages
class DocumentInfo(BaseModel):
    space: str
    name: str
    title: str = ""
    type: str = ""
    status: str = ""
    uri: str = ""


class PageInfo(BaseModel):
    space: str
    name: str
    title: str = ""
    kind: str = ""                # rich page / wiki page


# ---------------------------------------------------------------- results
class SectionResult(BaseModel):
    area: str                     # e.g. "roles", "fields"
    title: str
    status: SectionStatus
    providers: list[ProviderKind] = Field(default_factory=list)
    # named lists of the models above, stored as plain dicts so snapshots round-trip through JSON
    items: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    gaps: list[Gap] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_ms: float | None = None

    def count(self) -> int:
        return sum(len(v) for v in self.items.values())

    def put(self, name: str, models: list[BaseModel]) -> None:
        self.items[name] = [m.model_dump(mode="json", by_alias=True) for m in models]


class ConfigSnapshot(BaseModel):
    environment_id: str
    project_id: str
    release: str | None
    adapter: str
    taken_at: datetime = Field(default_factory=utcnow)
    sections: list[SectionResult] = Field(default_factory=list)

    def section(self, area: str) -> SectionResult | None:
        return next((s for s in self.sections if s.area == area), None)
