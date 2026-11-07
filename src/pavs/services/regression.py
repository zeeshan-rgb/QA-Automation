"""Regression comparison between two test runs of the same project: what got worse, what was fixed, what changed."""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.testing import CheckResult, TestRunRecord

RANK = {"pass": 0, "partial_pass": 1, "blocked": 2, "not_testable": 2, "fail": 3, "error": 3}
ORDER = {"regression": 0, "new_problem": 1, "changed": 2, "fixed": 3, "new": 4, "removed": 5, "same": 6}


@dataclass
class Change:
    kind: str                       # regression | new_problem | changed | fixed | new | removed | same
    group: str
    name: str
    requirement: str
    old_status: str
    new_status: str
    old_actual: str
    new_actual: str


def _key(r: CheckResult) -> tuple:
    if r.group == "Requirements":
        return (r.group, r.requirement_label, r.name)
    return (r.group, r.check_id)


def diff_runs(old: TestRunRecord, new: TestRunRecord) -> list[Change]:
    a = {_key(r): r for r in old.results}
    b = {_key(r): r for r in new.results}
    out: list[Change] = []
    for k in sorted(set(a) | set(b), key=str):
        x, y = a.get(k), b.get(k)
        ref = y or x
        name = ref.name if ref.group != "Requirements" else ref.name
        if x is None:
            kind = "new_problem" if RANK[y.status.value] >= 1 and y.status.value in ("fail", "error") else "new"
            out.append(Change(kind, y.group, name, y.requirement_label, "-", y.status.value, "", y.actual))
        elif y is None:
            out.append(Change("removed", x.group, name, x.requirement_label, x.status.value, "-", x.actual, ""))
        else:
            rx, ry = RANK[x.status.value], RANK[y.status.value]
            if ry > rx:
                kind = "regression"
            elif ry < rx:
                kind = "fixed"
            elif x.actual != y.actual:
                kind = "changed"
            else:
                kind = "same"
            out.append(Change(kind, y.group, name, y.requirement_label, x.status.value, y.status.value, x.actual, y.actual))
    out.sort(key=lambda c: (ORDER[c.kind], c.group, c.name))
    return out


def summarize(changes: list[Change]) -> dict[str, int]:
    s: dict[str, int] = {}
    for c in changes:
        s[c.kind] = s.get(c.kind, 0) + 1
    return s
