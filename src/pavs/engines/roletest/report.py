"""Turn probes + expectations into check results (for the normal history and exports) and a blockers report."""
from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.enums import TestStatus
from ...domain.testing import CheckResult, Severity
from .expectations import Expect, Matrix, key
from .model import ADVICE, Cause, Outcome, Probe
from .runner import TypeSpec

# actions the sheet has no column for: judged like the action they are a kind of
BORROWS = {"MODIFY FIELD (description)": "MODIFY", "ADD LINK": "MODIFY", "CHANGE STATUS (workflow)": None}


@dataclass
class Finding:
    group: str
    action: str
    kind: str                  # blocker | unexpected | untestable
    cause: Cause
    what: str
    detail: str
    fix: str


@dataclass
class RoleTestReport:
    user: str
    roles: list[str]
    project: str
    expectation: str                           # text: where the expected results came from
    results: list[CheckResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "blocker"]

    @property
    def passed(self) -> bool:
        return not self.blockers

    def root_causes(self) -> list[tuple[tuple[Cause, str], int]]:
        """The non-permission causes behind the blockers, with how many checks each one explains (largest first)."""
        tally: dict[tuple[Cause, str], int] = {}
        for f in self.blockers:
            if f.cause not in (Cause.PERMISSION, Cause.NONE):
                k = (f.cause, f.detail.split("; ")[0][:160])
                tally[k] = tally.get(k, 0) + 1
        return sorted(tally.items(), key=lambda kv: -kv[1])

    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "blocker": 0, "unexpected": 0, "untestable": 0}
        for r in self.results:
            if r.status == TestStatus.PASS:
                out["pass"] += 1
        for f in self.findings:
            out[f.kind] += 1
        return out

    def text(self) -> str:
        c = self.counts()
        head = [f"Role test of {self.user} as {', '.join(self.roles) or '(no role)'} in {self.project}",
                f"Expected results: {self.expectation}",
                f"{'PASSED: the user can do everything that was expected.' if self.passed else 'NOT PASSED: ' + str(c['blocker']) + ' blocker(s).'}  "
                f"{c['pass']} checks passed, {c['unexpected']} unexpected access, {c['untestable']} not testable."]
        out = head + [""]
        roots = self.root_causes()
        if roots:
            out.append("WHAT STANDS IN THE WAY (grouped by cause)")
            for (cause, detail), n in roots:
                out.append(f"  - {n} check(s): {cause.value}: {detail}")
                out.append(f"      Fix: {ADVICE.get(cause, '')}")
            out.append("")
        for title, kind in (("BLOCKERS (expected to work, but did not)", "blocker"),
                            ("UNEXPECTED ACCESS (works, but the sheet does not grant it)", "unexpected"),
                            ("NOT TESTABLE", "untestable")):
            rows = [f for f in self.findings if f.kind == kind]
            if not rows:
                continue
            out.append(title)
            for f in rows:
                shown = f.detail if f.cause in (Cause.PERMISSION, Cause.NONE) else f.cause.value     # a cause listed above is not repeated in full
                out.append(f"  - {f.group} / {f.action}: {f.what}" + (f" ({shown})" if shown and kind != "unexpected" else ""))
            if kind == "blocker" and any(f.cause == Cause.PERMISSION for f in rows):
                out.append(f"  Fix for the permission blockers: {ADVICE[Cause.PERMISSION]}")
            out.append("")
        out += [f"Note: {n}" for n in self.notes]
        return "\n".join(out).rstrip() + "\n"


def evaluate(p: Probe, expect: Expect) -> tuple[TestStatus, str, str]:
    """(status, kind of finding or '', what happened in words)."""
    o = p.outcome
    if expect in (Expect.YES, Expect.INFO):
        if o == Outcome.ALLOWED:
            return TestStatus.PASS, "", "worked"
        if o == Outcome.DENIED:
            return (TestStatus.FAIL if expect == Expect.YES else TestStatus.PARTIAL_PASS), ("blocker" if expect == Expect.YES else ""), "refused: the role has no permission"
        if expect == Expect.YES:
            return (TestStatus.BLOCKED if o == Outcome.BLOCKED else TestStatus.ERROR), "blocker", f"could not be done: {p.cause.value}"
        return TestStatus.NOT_TESTABLE, "untestable", f"could not be tried: {p.cause.value}"
    # NO / BLANK: it should be refused
    if o == Outcome.DENIED:
        return TestStatus.PASS, "", "refused, as expected"
    if o == Outcome.ALLOWED:
        if expect == Expect.NO:
            return TestStatus.FAIL, "unexpected", "worked, but the sheet says No"
        return TestStatus.PARTIAL_PASS, "unexpected", "worked, but the sheet does not grant it"
    return TestStatus.NOT_TESTABLE, "untestable", f"could not be tried: {p.cause.value}"


def build_report(user: str, roles: list[str], project: str, probes: list[Probe], types: list[TypeSpec],
                 matrix: Matrix | None, notes: list[str] | None = None) -> RoleTestReport:
    if matrix is not None:
        expectation = f"sheet '{matrix.sheet}' ({', '.join(r for r in roles if matrix.has_role(r)) or 'no matching role'})"
    else:
        expectation = "everything the user is asked to do should work (no Excel sheet loaded)"
    rep = RoleTestReport(user, roles, project, expectation, notes=list(notes or []))
    group_of = {t.group: (matrix.match_group(t.id, t.name) if matrix else None) for t in types}
    for i, p in enumerate(probes, 1):
        if matrix is None:
            e = Expect.YES
        else:
            if p.group in group_of:
                gkey = group_of[p.group]                                           # a Work Item type
            else:
                gkey = key(p.group) if key(p.group) in matrix.group_names else None   # DOCUMENT, TEST RUN, ...
            action = BORROWS.get(p.action, p.action)
            e = Expect.INFO if action is None else matrix.expect(roles, gkey, action)
        status, kind, what = evaluate(p, e)
        fix = ADVICE.get(p.cause, "") if kind in ("blocker", "untestable") else ""
        if kind == "unexpected":
            fix = "Review whether the role should be able to do this; remove the permission if not."
        exp_text = {Expect.YES: "allowed", Expect.NO: "refused (sheet: No)", Expect.BLANK: "refused (not granted in the sheet)",
                    Expect.INFO: "no statement in the sheet"}[e]
        rep.results.append(CheckResult(
            check_id=f"R{i:03d}", group=f"Role test: {p.group}", name=f"{p.action} as {user}", status=status,
            severity=Severity.ERROR if status in (TestStatus.FAIL, TestStatus.BLOCKED, TestStatus.ERROR) else Severity.INFO,
            expected=exp_text, actual=f"{p.outcome.value}: {what}", mechanism=p.mechanism,
            evidence=[x for x in (p.detail, fix) if x]))
        if kind:
            rep.findings.append(Finding(p.group, p.action, kind, p.cause, what, p.detail, fix))
    order = {"blocker": 0, "unexpected": 1, "untestable": 2}
    rep.findings.sort(key=lambda f: order[f.kind])
    return rep
