"""Built-in project health checks: consistency of the configuration, no requirements needed.

Each check reads the inspected snapshot and reports PASS, FAIL (errors), PARTIAL_PASS (only
warnings) or BLOCKED (the data it needs could not be read - the reason and fix are in the
result). Nothing here changes anything on the server.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from ...domain.enums import TestStatus
from ...domain.testing import CheckResult, Severity
from .view import BUILTIN_FIELDS, SPECIAL_ROLES, SPECIAL_STATES, ConfigView, norm

FORBIDDEN_ID_CHARS = re.compile(r"""[\\/:*?"<>|#';$^%~!`\s]""")
LIST_LIMIT = 25
SYSTEM_ENUMS = {"oslc-link-role", "hyperlink-role"}


@dataclass
class Health:
    id: str
    name: str
    description: str
    needs: list[tuple[str, str]]                 # (area, list name) that must have data
    run: Callable[[ConfigView], tuple[list[str], list[str], str]]   # -> (errors, warnings, actual-when-ok)


def _blocked(view: ConfigView, check: Health) -> CheckResult | None:
    for area, name in check.needs:
        if not view.has_data(area, name):
            return CheckResult(
                check_id=check.id, group="Health checks", name=check.name, status=TestStatus.BLOCKED,
                expected=check.description, actual="not checked: required configuration could not be read",
                evidence=[view.why_missing(area)], mechanism="configuration snapshot")
    return None


def _trim(items: list[str]) -> list[str]:
    return items[:LIST_LIMIT] + ([f"... and {len(items) - LIST_LIMIT} more"] if len(items) > LIST_LIMIT else [])


# ---------------------------------------------------------------- the checks
def h_workflow_per_type(v: ConfigView):
    have = {w["target_type"] for w in v.workflows() if w["prototype"] == "WorkItem"}
    missing = [t["id"] for t in v.types() if t["id"] not in have]
    return [f"Work Item type '{t}' has no workflow (neither type-specific nor generic)" for t in missing], [], \
        f"all {len(v.types())} Work Item types have a workflow"


def h_workflow_statuses(v: ConfigView):
    errs, warns = [], []
    for w in v.workflows():
        if w["prototype"] != "WorkItem":
            continue
        known = {o["id"] for o in (v.enum_options("status", w["target_type"]) or [])}
        if not known:
            continue
        for s in w["statuses_list"]:
            if s not in known:
                errs.append(f"{w['target_type']}: workflow uses status '{s}' that is not in the status enumeration "
                            f"({w['sources'][0]['location']})")
        for o in known - set(w["statuses_list"]):
            warns.append(f"{w['target_type']}: status '{o}' is in the enumeration but never used by the workflow")
    return errs, warns, "every workflow status exists in its status enumeration"


def h_transition_actions(v: ConfigView):
    errs = []
    for w in v.workflows():
        defined = {a["id"] for a in w["actions"]}
        for t in w["transitions"]:
            if t["action"] not in defined:
                errs.append(f"{w['prototype']} {w['target_type'] or 'generic'}: transition '{t['action']}' "
                            f"({t['from']} -> {t['to']}) has no <action> definition")
        used = {t["action"] for t in w["transitions"]} | {w.get("initial_action")}
        for a in w["actions"]:
            if a["id"] not in used:
                errs.append(f"{w['prototype']} {w['target_type'] or 'generic'}: action '{a['id']}' is defined "
                            "but no transition uses it")
    return list(dict.fromkeys(errs)), [], "every transition has an action definition and every action is used"


def h_initial_status(v: ConfigView):
    errs = []
    for w in v.workflows():
        if not w.get("initial_status"):
            errs.append(f"{w['prototype']} {w['target_type'] or 'generic'}: workflow has no initial status")
    return errs, [], "every workflow defines an initial status"


def h_reachability(v: ConfigView):
    errs, warns = [], []
    for w in v.workflows():
        init = w.get("initial_status")
        if not init:
            continue
        reach, todo = {init}, [init]
        while todo:
            cur = todo.pop()
            for t in w["transitions"]:
                if t["from"] == cur and t["to"] not in reach:
                    reach.add(t["to"])
                    todo.append(t["to"])
        label = f"{w['prototype']} {w['target_type'] or 'generic'}"
        for s in w["statuses_list"]:
            if s not in reach:
                errs.append(f"{label}: status '{s}' cannot be reached from the initial status '{init}'")
        terminal = {o["id"] for o in (v.enum_options("status", w["target_type"]) or []) if o.get("terminal")}
        for s in w["statuses_list"]:
            if not any(t["from"] == s for t in w["transitions"]) and s not in terminal:
                warns.append(f"{label}: status '{s}' has no outgoing transition (dead end)")
    return errs, warns, "all statuses are reachable and none is a dead end"


def h_workflow_fields(v: ConfigView):
    errs = []
    for w in v.workflows():
        if w["prototype"] != "WorkItem":
            continue
        known = v.field_ids(w["target_type"]) | {"resolution", "assignee", "version"}
        for a in w["actions"]:
            for kind, names in (("requires", a["required_fields"]), ("clears", a["cleared_fields"])):
                for f in names:
                    if f not in known:
                        errs.append(f"{w['target_type']}: action '{a['id']}' {kind} field '{f}' which is not defined "
                                    "for this type")
    return errs, [], "all fields used by workflow actions exist"


def h_field_enums(v: ConfigView):
    known = {e["enum_id"] for e in v.enumerations()}
    errs = []
    for f in v.items("fields", "layers") + v.items("fields", "effective"):
        eid = f.get("enum_id")
        if not eid or eid.startswith("@"):
            continue
        if eid not in known and eid not in {"status", "severity", "priority", "resolution"}:
            errs.append(f"field '{f['id']}' ({f.get('target_type') or 'all types'}) uses enumeration '{eid}' "
                        "that does not exist")
    return list(dict.fromkeys(errs)), [], "every enumeration used by a custom field exists"


def h_enum_options(v: ConfigView):
    errs, warns = [], []
    for e in v.enumerations():
        label = f"{e['enum_id']}{'@' + e['target_type'] if e.get('target_type') else ''}"
        if e["enum_id"] in SYSTEM_ENUMS:      # system enumerations legitimately use URIs as ids
            continue
        ids = [o["id"] for o in e["options"]]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            errs.append(f"enumeration {label}: option id '{dup}' appears more than once")
        for i in ids:
            if FORBIDDEN_ID_CHARS.search(i):
                errs.append(f"enumeration {label}: option id '{i}' contains a forbidden character")
        defaults = [o["id"] for o in e["options"] if o.get("default")]
        merged = any("getAllEnumOptionsForId" in (s.get("location") or "") for s in e.get("sources") or [])
        if len(defaults) > 1 and not merged:      # a merged all-contexts listing has one default per context: not a defect
            errs.append(f"enumeration {label}: more than one default option ({', '.join(defaults)})")
        if not ids:
            warns.append(f"enumeration {label} has no options")
    return errs, warns, "enumeration options are unique, valid and have at most one default"


def h_field_ids(v: ConfigView):
    errs = []
    by_type: dict[str, dict[str, list[str]]] = {}
    for f in v.items("fields", "layers"):
        by_type.setdefault(f.get("target_type") or "(all types)", {}).setdefault(f["id"], []).append(f["scope"])
    for t, fields in by_type.items():
        for fid, scopes in fields.items():
            if fid in BUILTIN_FIELDS:
                errs.append(f"{t}: custom field id '{fid}' clashes with a built-in field")
            if len(scopes) > 1 and len(set(scopes)) == 1:
                errs.append(f"{t}: custom field '{fid}' is defined twice in the same scope ({scopes[0]})")
    return errs, [], "custom field ids are unique and do not clash with built-in fields"


def h_link_roles(v: ConfigView):
    errs, warns = [], []
    types = {t["id"] for t in v.types()}
    for r in v.link_roles():
        if not r.get("opposite_name"):
            warns.append(f"link role '{r['id']}' has no opposite name")
        for frm, to in r.get("rules", []):
            for t in [x.strip() for x in f"{frm},{to}".split(",")]:
                if t and t not in types and t != "__other":   # __other = Polarion wildcard
                    errs.append(f"link role '{r['id']}': rule refers to unknown Work Item type '{t}'")
    return list(dict.fromkeys(errs)), warns, "link roles have opposite names and only refer to existing types"


def h_assignments(v: ConfigView):
    glob = {r["id"] for r in v.roles() if r["scope"] == "global"}
    proj = {r["id"] for r in v.roles() if r["scope"] == "project"}
    errs = []
    for a in v.items("roles", "assignments"):
        if a["scope"] == "effective":
            continue
        ok = (glob if a["scope"] == "global" else proj) | SPECIAL_ROLES
        if a["role"] not in ok:
            errs.append(f"{a['scope']} role assignment: {a['principal_kind']} '{a['principal']}' has role "
                        f"'{a['role']}' which is not a defined {a['scope']} role")
    return list(dict.fromkeys(errs)), [], "all role assignments refer to defined roles"


def h_customsets(v: ConfigView):
    defined = v.customset_ids()
    errs = []
    for r in v.permission_rules():
        for ref in re.findall(r"@([A-Za-z0-9_\-]+)", r["permission"]):
            if ref not in defined:
                errs.append(f"{r['scope']} permissions: rule for role '{r['role']}' refers to customset "
                            f"'@{ref}' which is not defined")
    return list(dict.fromkeys(errs)), [], "all customset references in permissions.xml are defined"


def h_document_types(v: ConfigView):
    types = {t["id"] for t in v.document_types()}
    have_wf = {w["target_type"] for w in v.workflows() if w["prototype"] == "Module"}
    errs, warns = [], []
    for d in v.items("documents", "documents"):
        if d.get("type") and d["type"] not in types:
            errs.append(f"document '{d['space']}/{d['name']}' has unknown type '{d['type']}'")
    for t in sorted(types - have_wf):
        warns.append(f"document type '{t}' has no workflow")
    return errs, warns, "document types exist and have workflows"


def h_read_only_rules(v: ConfigView):
    errs = []
    for r in v.items("field_rules", "rules"):
        if r["kind"] != "read_only" or r["ref"] in SPECIAL_STATES:
            continue
        t = r.get("target_type")
        if t and t in {x["id"] for x in v.types()}:   # rules for types this project does not use are inherited noise
            known = {o["id"] for o in v.statuses(t)}
            if known and r["ref"] not in known:
                errs.append(f"read-only rule for '{t}' refers to unknown status '{r['ref']}'")
    return list(dict.fromkeys(errs)), [], "read-only field rules only refer to existing statuses"


CHECKS = [
    Health("H01", "Every Work Item type has a workflow", "each type resolves to a workflow file",
           [("types", "types"), ("workflow", "effective")], h_workflow_per_type),
    Health("H02", "Workflow statuses exist in the status enumeration", "no workflow status is missing from its enumeration",
           [("workflow", "effective"), ("enumerations", "enumerations")], h_workflow_statuses),
    Health("H03", "Workflow transitions and actions agree", "every transition has an action and every action is used",
           [("workflow", "effective")], h_transition_actions),
    Health("H04", "Workflows define an initial status", "initial status present",
           [("workflow", "effective")], h_initial_status),
    Health("H05", "Workflow statuses are reachable", "no unreachable status, no dead end",
           [("workflow", "effective")], h_reachability),
    Health("H06", "Fields used by workflow actions exist", "required/cleared fields are defined for the type",
           [("workflow", "effective"), ("types", "types")], h_workflow_fields),
    Health("H07", "Custom field enumerations exist", "each enum field points at an existing enumeration",
           [("enumerations", "enumerations"), ("fields", "layers")], h_field_enums),
    Health("H08", "Enumerations are well formed", "unique valid option ids, at most one default",
           [("enumerations", "enumerations")], h_enum_options),
    Health("H09", "Custom field ids are valid", "unique, no clash with built-in fields",
           [("fields", "layers")], h_field_ids),
    Health("H10", "Link roles are consistent", "opposite names present, rules use existing types",
           [("links", "link_roles"), ("types", "types")], h_link_roles),
    Health("H11", "Role assignments use defined roles", "no assignment refers to an unknown role",
           [("roles", "assignments"), ("roles", "roles")], h_assignments),
    Health("H12", "Permission customsets are defined", "no rule refers to an undefined customset",
           [("permissions", "rules")], h_customsets),
    Health("H13", "Documents use defined types with workflows", "document types exist and have a workflow",
           [("documents", "document_types")], h_document_types),
    Health("H14", "Read-only field rules use existing statuses", "no rule refers to an unknown status",
           [("field_rules", "rules"), ("types", "types")], h_read_only_rules),
]


def run_health_checks(view: ConfigView) -> list[CheckResult]:
    out = []
    for c in CHECKS:
        started = time.perf_counter()
        blocked = _blocked(view, c)
        if blocked is not None:
            out.append(blocked)
            continue
        try:
            errs, warns, ok_text = c.run(view)
        except Exception as exc:   # a check bug must never hide the other checks
            out.append(CheckResult(check_id=c.id, group="Health checks", name=c.name, status=TestStatus.ERROR,
                                   expected=c.description, actual=f"the check itself failed: {type(exc).__name__}: {exc}"))
            continue
        status = TestStatus.FAIL if errs else TestStatus.PARTIAL_PASS if warns else TestStatus.PASS
        sev = Severity.ERROR if errs else Severity.WARNING if warns else Severity.INFO
        actual = (f"{len(errs)} problem(s)" + (f", {len(warns)} warning(s)" if warns else "")) if errs else \
            (f"{len(warns)} warning(s)" if warns else ok_text)
        out.append(CheckResult(check_id=c.id, group="Health checks", name=c.name, status=status, severity=sev,
                               expected=c.description, actual=actual, mechanism="configuration snapshot",
                               evidence=_trim(errs) + _trim([f"warning: {w}" for w in warns]),
                               duration_ms=round((time.perf_counter() - started) * 1000, 1)))
    return out
