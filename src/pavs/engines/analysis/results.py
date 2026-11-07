"""Turn analyzer findings into test results (one result per rule, group "Scripts")."""
from __future__ import annotations

from ...domain.enums import TestStatus
from ...domain.testing import CheckResult, Severity
from .analyzer import RULES, SourceFile, analyze_files, find_node, release_code

LIST_LIMIT = 25
SEV = {"error": Severity.ERROR, "warning": Severity.WARNING, "info": Severity.INFO}


def analyze_to_results(files: list[SourceFile], target_release: str | None, label: str) -> list[CheckResult]:
    kinds = {f.kind for f in files}
    n_js, n_vm = sum(f.kind == "js" for f in files), sum(f.kind == "vm" for f in files)
    scanned = f"{label}: {n_js} script(s), {n_vm} template(s)/block(s) analysed"
    if not files:
        return [CheckResult(check_id="S00", group="Scripts", name="Script and template analysis", status=TestStatus.BLOCKED,
                            expected="scripts and templates to analyse", actual="no scripts or templates were found",
                            evidence=[f"{label}: nothing to analyse. Enable the repository reader (read-only) or choose a script folder."],
                            mechanism="static analysis")]
    findings, notes = analyze_files(files, target_release)
    tgt = release_code(target_release)
    out: list[CheckResult] = []
    for rule in RULES.values():
        if not set(rule.kinds) & kinds:
            continue
        hits = [f for f in findings if f.rule == rule.id]
        ev = [scanned]
        if rule.id == "S04" and not tgt:
            out.append(CheckResult(check_id=rule.id, group="Scripts", name=rule.title, status=TestStatus.BLOCKED, severity=SEV[rule.severity],
                                   expected=rule.why, actual="the target Polarion release is unknown", evidence=ev + [
                                       "Connect to a server first so its release is known."], mechanism="static analysis + SDK change log"))
            continue
        if rule.id == "S01" and not find_node() and n_js:
            out.append(CheckResult(check_id=rule.id, group="Scripts", name=rule.title, status=TestStatus.BLOCKED, severity=SEV[rule.severity],
                                   expected=rule.why, actual="Node.js was not found", evidence=ev + [
                                       "Install Node.js (or set PAVS_NODE) to check JavaScript syntax."], mechanism="node --check"))
            continue
        lines = [f"{h.where()}: {h.message}" for h in hits[:LIST_LIMIT]] + \
                ([f"... and {len(hits) - LIST_LIMIT} more"] if len(hits) > LIST_LIMIT else [])
        if not hits:
            status, actual = TestStatus.PASS, "no problems found"
        elif rule.severity == "error":
            status, actual = TestStatus.FAIL, f"{len(hits)} problem(s) in {len({h.file for h in hits})} file(s)"
        else:
            status, actual = TestStatus.PARTIAL_PASS, f"{len(hits)} finding(s) in {len({h.file for h in hits})} file(s)"
        out.append(CheckResult(check_id=rule.id, group="Scripts", name=rule.title, status=status,
                               severity=SEV[rule.severity] if hits else Severity.INFO, expected=rule.why, actual=actual,
                               mechanism="static analysis" + (" + SDK change log" if rule.id == "S04" else ""),
                               evidence=ev + lines))
    for note in notes:
        out[0].evidence.append(note) if out else None
    return out
