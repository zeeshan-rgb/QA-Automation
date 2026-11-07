"""Runs health checks and requirement checks against one inspected project."""
from __future__ import annotations

from typing import Callable

from ...domain.config import ConfigSnapshot
from ...domain.requirements import Requirement, TestCase
from ...domain.testing import CheckResult, TestRunRecord
from .executor import Executor, ServerPermission
from .data import DataSet, run_data_checks
from .health import run_health_checks
from .view import ConfigView


def inspection_gaps(snap: ConfigSnapshot) -> list[str]:
    out = []
    for s in snap.sections:
        for g in s.gaps:
            out.append(f"{s.title}: {g.item} - {g.reason}" + (f" ({g.remedy})" if g.remedy else ""))
        for e in s.errors:
            out.append(f"{s.title}: {e}")
    return out


def run_checks(snap: ConfigSnapshot, *, health: bool, cases: list[TestCase] | None = None,
               requirements: dict[str, Requirement] | None = None,
               server_permission: ServerPermission | None = None, soap_available: bool = False,
               progress: Callable[[str, int], None] | None = None, dataset: DataSet | None = None,
               behaviour=None, user_id: str | None = None, data_checks: bool = False) -> list[CheckResult]:
    report = progress or (lambda m, p: None)
    view = ConfigView(snap)
    results: list[CheckResult] = []
    if health:
        report("Running health checks", 10)
        results += run_health_checks(view)
    if data_checks and dataset is not None:
        report("Checking the Work Items", 30)
        results += run_data_checks(view, dataset)
    cases = [c for c in (cases or []) if c.status.value != "rejected"]
    if cases:
        ex = Executor(view, server_permission, soap_available, dataset=dataset, behaviour=behaviour, user_id=user_id)
        reqs = requirements or {}
        for i, tc in enumerate(cases):
            report(f"Checking requirement tests ({i + 1}/{len(cases)})", 40 + int(55 * i / max(len(cases), 1)))
            label = reqs[tc.requirement_id].label if tc.requirement_id in reqs else ""
            results.append(ex.run(tc, label))
    return results


def new_record(snap: ConfigSnapshot, env_name: str, server_url: str, kind: str, user: str = "",
               requirement_set: str = "", snapshot_id: int | None = None) -> TestRunRecord:
    return TestRunRecord(environment_id=snap.environment_id, environment_name=env_name, server_url=server_url,
                         project_id=snap.project_id, release=snap.release, adapter=snap.adapter, kind=kind,
                         requirement_set=requirement_set, snapshot_id=snapshot_id,
                         inspection_gaps=inspection_gaps(snap), user=user)
