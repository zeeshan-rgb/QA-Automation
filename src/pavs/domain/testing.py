"""Test results and runs (no AI: results come only from executed checks with evidence)."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .enums import TestStatus
from .models import utcnow


class Severity(str, Enum):
    ERROR = "error"        # the configuration is wrong or inconsistent
    WARNING = "warning"    # suspicious but may be intended
    INFO = "info"


class CheckResult(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    check_id: str                       # H03 for built-in checks, the test case id for requirement checks
    group: str                          # "Health checks" | "Requirements"
    name: str
    status: TestStatus
    severity: Severity = Severity.ERROR
    requirement_id: str = ""
    requirement_label: str = ""
    criterion: str = ""
    expected: str = ""
    actual: str = ""
    mechanism: str = ""                 # e.g. "SVN workflow files" or "SOAP Security.hasPermission"
    evidence: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0


class TestRunRecord(BaseModel):
    __test__ = False
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    environment_id: str
    environment_name: str = ""
    server_url: str = ""
    project_id: str
    release: str | None = None
    adapter: str = ""
    kind: str = "health"                # health | requirements | all
    requirement_set: str = ""
    snapshot_id: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    results: list[CheckResult] = Field(default_factory=list)
    inspection_gaps: list[str] = Field(default_factory=list)   # what could not be inspected, with the fix
    user: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in TestStatus}
        for r in self.results:
            out[r.status.value] += 1
        return out

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def verdict(self) -> str:
        c = self.counts()
        if c["fail"] or c["error"]:
            return "FAILED"
        if c["partial_pass"] or c["blocked"] or c["not_testable"]:
            return "PASSED WITH GAPS"
        return "PASSED" if self.results else "NO TESTS"

    @property
    def coverage_pct(self) -> float:
        """Share of checks that reached a real verdict (pass or fail), not blocked/not testable."""
        if not self.results:
            return 0.0
        decided = sum(1 for r in self.results if r.status in (TestStatus.PASS, TestStatus.FAIL, TestStatus.PARTIAL_PASS))
        return round(100.0 * decided / len(self.results), 1)


class TestArtifact(BaseModel):
    """A Work Item this tool created in a test project. Recorded BEFORE the next step so a crash leaves a trail."""
    __test__ = False
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_id: str
    environment_id: str
    project_id: str
    ref: str                            # Work Item id inside the project, e.g. AD-17
    title: str
    created_at: datetime = Field(default_factory=utcnow)
    deleted_at: datetime | None = None
    note: str = ""
