"""Requirements, acceptance criteria and test cases (Phase 3).

A TestCase holds a machine-readable CheckSpec: the contract the Phase 4 execution engine runs.
Nothing in this module produces a result. Test cases come from fixed rules or a structured
specification, never from a model. Results only come from executed checks with evidence.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .enums import TestOrigin
from .models import utcnow


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Requirement(BaseModel):
    id: str = Field(default_factory=_id)
    set_id: str = ""
    external_id: str = ""            # id from the source document, e.g. REQ-12
    title: str = ""
    text: str
    acceptance_criteria: list[str] = Field(default_factory=list)   # as written in the source
    source: str = ""                 # file name + location (row 4, paragraph 12, ...)
    attributes: dict[str, str] = Field(default_factory=dict)       # extra columns from the source
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def label(self) -> str:
        return self.external_id or self.title[:40] or self.id


class RequirementSet(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    source_file: str = ""
    source_format: str = ""
    imported_at: datetime = Field(default_factory=utcnow)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- checks (the Phase 4 contract)
class CheckKind(str, Enum):
    ROLE_EXISTS = "role_exists"                     # target.role
    ROLE_ASSIGNED = "role_assigned"                 # target.role, target.user
    PERMISSION = "permission"                       # target.role|user, target.permission  -> expect granted/denied
    TYPE_EXISTS = "type_exists"                     # target.type
    FIELD_EXISTS = "field_exists"                   # target.type?, target.field, target.field_type?
    FIELD_REQUIRED = "field_required"               # target.type?, target.field
    FIELD_READ_ONLY = "field_read_only"             # target.type?, target.field, target.status?
    ENUM_CONTAINS = "enum_contains"                 # target.enum (or field), target.options[]
    STATUS_EXISTS = "status_exists"                 # target.type, target.status
    TRANSITION = "transition"                       # target.type, target.from_status, target.to_status, target.action?
    ACTION_REQUIRES_FIELD = "action_requires_field"  # target.type, target.action, target.field
    LINK_ROLE_EXISTS = "link_role_exists"           # target.link_role, target.from_type?, target.to_type?
    DOCUMENT_TYPE_EXISTS = "document_type_exists"   # target.document_type
    TRACEABILITY = "traceability"                   # target.from_type, target.to_type, target.link_role?
    CUSTOM = "custom"                               # free-form: not executable automatically


class Expectation(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    GRANTED = "granted"
    DENIED = "denied"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    REQUIRED = "required"
    OPTIONAL = "optional"
    COVERED = "covered"                 # traceability: every source item is linked


class Polarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"


class CheckSpec(BaseModel):
    kind: CheckKind
    expect: Expectation
    target: dict[str, Any] = Field(default_factory=dict)
    note: str = ""

    def describe(self) -> str:
        t = self.target
        parts = [f"{k}={v}" for k, v in t.items() if v not in (None, "", [])]
        return f"{self.kind.value}({', '.join(parts)}) -> {self.expect.value}"


class Testability(str, Enum):
    EXECUTABLE = "executable"           # a usable provider exists on the chosen environment
    BLOCKED = "blocked"                 # possible in principle, something missing (provider, login, account)
    NOT_TESTABLE = "not_testable"       # no mechanism can observe this
    UNKNOWN = "unknown"                 # no environment chosen yet


class TestCaseStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class TestCase(BaseModel):
    id: str = Field(default_factory=_id)
    requirement_id: str
    criterion: str = ""                 # the acceptance criterion this case covers
    title: str
    polarity: Polarity = Polarity.POSITIVE
    check: CheckSpec
    origin: TestOrigin = TestOrigin.RULE_GENERATED
    status: TestCaseStatus = TestCaseStatus.PROPOSED
    testability: Testability = Testability.UNKNOWN
    testability_detail: str = ""
    mechanism: str = ""                 # how Phase 4 will check it (capability + provider)
    rationale: str = ""                 # which rule produced this case
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class FindingKind(str, Enum):
    MISSING_CRITERIA = "missing_criteria"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    UNRESOLVED_REFERENCE = "unresolved_reference"   # name not found in the inspected configuration
    NOT_AUTOMATABLE = "not_automatable"


class Finding(BaseModel):
    requirement_id: str
    kind: FindingKind
    message: str
    related: list[str] = Field(default_factory=list)  # other requirement ids (conflicts)
    origin: TestOrigin = TestOrigin.RULE_GENERATED


class ConfigRef(BaseModel):
    """A Polarion configuration element a requirement maps to."""
    kind: str                           # role, permission, type, field, enum, status, action, link_role, document_type
    name: str                           # as written in the requirement
    resolved_id: str | None = None      # id found in the inspected configuration
    in_snapshot: bool | None = None     # None = no snapshot available (informational, NOT a result)
    area: str = ""                      # Inspector section that holds it


class Analysis(BaseModel):
    """Everything derived from one requirement. Rebuilt on every analysis run."""
    requirement_id: str
    criteria: list[str] = Field(default_factory=list)
    refs: list[ConfigRef] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    analyzed_at: datetime = Field(default_factory=utcnow)
