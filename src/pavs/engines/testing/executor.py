"""Executes generated checks against the inspected configuration (and the server, where it can).

Verdicts: PASS / FAIL (a real comparison was made), PARTIAL_PASS (checked from configuration
only, or only part of it could be checked), BLOCKED (the data or access needed is missing:
the reason and fix are in the evidence), NOT_TESTABLE (no mechanism can observe it).
Every check is read-only.
"""
from __future__ import annotations

import time
from typing import Callable

from ...domain.enums import TestStatus
from ...domain.requirements import CheckKind, CheckSpec, Expectation, TestCase
from ...domain.testing import CheckResult
from .data import DataSet, coverage
from .view import SPECIAL_ROLES, ConfigView, norm

# (user, permission) -> True/False from the server, None if it could not be asked
ServerPermission = Callable[[str, str], bool | None]


def _result(tc: TestCase, status: TestStatus, expected: str, actual: str, mechanism: str,
            evidence: list[str] | None = None, requirement_label: str = "") -> CheckResult:
    return CheckResult(check_id=tc.id, group="Requirements", name=tc.title, status=status,
                       requirement_id=tc.requirement_id, requirement_label=requirement_label,
                       criterion=tc.criterion, expected=expected, actual=actual, mechanism=mechanism,
                       evidence=evidence or [])


class Executor:
    def __init__(self, view: ConfigView, server_permission: ServerPermission | None = None,
                 soap_available: bool = False, dataset: DataSet | None = None, behaviour=None,
                 user_id: str | None = None) -> None:
        self.v = view
        self.server_permission = server_permission
        self.soap = soap_available
        self.data = dataset
        self.behaviour = behaviour          # engines.testing.behaviour.Behaviour, only when writes are authorized
        self.user_id = user_id
        self.handlers = {
            CheckKind.ROLE_EXISTS: self._role_exists, CheckKind.ROLE_ASSIGNED: self._role_assigned,
            CheckKind.PERMISSION: self._permission_with_behaviour, CheckKind.TYPE_EXISTS: self._type_exists,
            CheckKind.FIELD_EXISTS: self._field_exists, CheckKind.FIELD_REQUIRED: self._field_required,
            CheckKind.FIELD_READ_ONLY: self._field_read_only, CheckKind.ENUM_CONTAINS: self._enum_contains,
            CheckKind.STATUS_EXISTS: self._status_exists, CheckKind.TRANSITION: self._transition_with_behaviour,
            CheckKind.ACTION_REQUIRES_FIELD: self._action_field, CheckKind.LINK_ROLE_EXISTS: self._link_role,
            CheckKind.DOCUMENT_TYPE_EXISTS: self._document_type, CheckKind.TRACEABILITY: self._traceability,
        }

    def run(self, tc: TestCase, requirement_label: str = "") -> CheckResult:
        started = time.perf_counter()
        spec = tc.check
        handler = self.handlers.get(spec.kind)
        if handler is None:
            res = _result(tc, TestStatus.NOT_TESTABLE, spec.describe(), "no automatic mechanism exists for this check",
                          "-", ["Test this one manually."])
        else:
            try:
                res = handler(tc, spec)
            except Exception as exc:     # a check bug must not stop the run
                res = _result(tc, TestStatus.ERROR, spec.describe(), f"the check itself failed: {type(exc).__name__}: {exc}", "-")
        res.requirement_label = requirement_label
        res.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return res

    # ------------------------------------------------------------------ helpers
    def _blocked(self, tc: TestCase, spec: CheckSpec, area: str, mechanism: str) -> CheckResult:
        return _result(tc, TestStatus.BLOCKED, spec.describe(), "not checked: required configuration could not be read",
                       mechanism, [self.v.why_missing(area)])

    @staticmethod
    def _present(tc: TestCase, spec: CheckSpec, found: bool, what: str, where: str, mech: str,
                 evidence: list[str] | None = None) -> CheckResult:
        want = spec.expect != Expectation.ABSENT
        ok = found == want
        exp = f"{what} {'exists' if want else 'does not exist'}"
        act = f"{what} {'exists' if found else 'does not exist'} ({where})" if where else \
            f"{what} {'exists' if found else 'does not exist'}"
        return _result(tc, TestStatus.PASS if ok else TestStatus.FAIL, exp, act, mech, evidence)

    # ------------------------------------------------------------------ behavioural add-ons
    @staticmethod
    def _merge(res: CheckResult, b, *, can_upgrade: bool) -> CheckResult:
        """Fold a behavioural result into a config/server result. A real failure always wins."""
        res.evidence = list(res.evidence) + ["Behavioural test: " + b.actual] + b.evidence
        if b.status == TestStatus.FAIL:
            res.status, res.actual, res.mechanism = TestStatus.FAIL, b.actual, b.mechanism
        elif b.status == TestStatus.PASS:
            if can_upgrade or res.status == TestStatus.PASS:
                res.status, res.mechanism = TestStatus.PASS, b.mechanism
                res.actual = b.actual
        else:   # blocked / error: keep what we had, and say so
            res.evidence.append("The behavioural test could not run, so this result rests on the checks above only.")
            if res.status == TestStatus.PASS:
                res.status = TestStatus.PARTIAL_PASS
        return res

    def _permission_with_behaviour(self, tc, spec):
        res = self._permission(tc, spec)
        if self.behaviour is None or spec.expect not in (Expectation.GRANTED, Expectation.DENIED):
            return res
        role = self.v.resolve_role(spec.target["role"])
        account = self.behaviour.accounts.get(role or "")
        parts = spec.target["permission"].rsplit(".", 2)
        if account is None or len(parts) < 3:
            if role and role not in SPECIAL_ROLES and res.status != TestStatus.FAIL:
                res.evidence.append(f"No test account is configured for role '{role}', so no behavioural test was run.")
            return res
        b = self.behaviour.permission_as(account, parts[-2], parts[-1], spec.expect == Expectation.GRANTED)
        return res if b is None else self._merge(res, b, can_upgrade=True)

    def _transition_with_behaviour(self, tc, spec):
        res = self._transition(tc, spec)
        if self.behaviour is None or spec.expect != Expectation.ALLOWED or res.status != TestStatus.PASS:
            return res
        tid, wfs, err = self._workflow_for(tc, spec)
        if err or not wfs:
            return res
        w = wfs[0]
        a = self.v.resolve_status(spec.target["from_status"], w["target_type"])
        b = self.v.resolve_status(spec.target["to_status"], w["target_type"])
        hit = next((t for t in w["transitions"] if t["from"] == a and t["to"] == b), None)
        if hit is None:
            return res
        act = next((x for x in w["actions"] if x["id"] == hit["action"]), {"required_fields": []})
        br = self.behaviour.transition(w["target_type"], a, b, hit["action"], act["required_fields"], self.user_id)
        return self._merge(res, br, can_upgrade=False)

    # ------------------------------------------------------------------ roles
    def _role_exists(self, tc, spec):
        if not self.v.has_data("roles", "roles"):
            return self._blocked(tc, spec, "roles", "role list")
        name = spec.target["role"]
        rid = self.v.resolve_role(name)
        roles = self.v.roles()
        hit = next((r for r in roles if r["id"] == rid), None)
        where = f"{hit['scope']} role '{rid}'" if hit else ""
        return self._present(tc, spec, rid is not None, f"Role '{name}'", where, "role list",
                             [f"Known roles: {', '.join(sorted({r['id'] for r in roles}))}"])

    def _role_assigned(self, tc, spec):
        if not self.v.has_data("roles", "assignments"):
            return self._blocked(tc, spec, "roles", "role assignments")
        role = self.v.resolve_role(spec.target["role"]) or spec.target["role"]
        user = spec.target["user"]
        ok = any(a["role"] == role and norm(a["principal"]) == norm(user) for a in self.v.items("roles", "assignments"))
        return _result(tc, TestStatus.PASS if ok else TestStatus.FAIL, f"user '{user}' has role '{role}'",
                       "assigned" if ok else "not assigned", "role assignments",
                       [f"Holders of '{role}': {', '.join(self.v.role_holders(role)) or 'nobody'}"])

    def _permission(self, tc, spec):
        t = spec.target
        role_name, perm = t["role"], t["permission"]
        want = spec.expect == Expectation.GRANTED
        exp = f"role '{role_name}' {'has' if want else 'does not have'} {perm.rsplit('.', 2)[-2]}.{perm.rsplit('.', 1)[-1]}"
        role = self.v.resolve_role(role_name)
        if role is None:
            if not self.v.has_data("roles", "roles"):
                return self._blocked(tc, spec, "roles", "role list")
            return _result(tc, TestStatus.FAIL, exp, f"role '{role_name}' does not exist", "role list",
                           [f"Known roles: {', '.join(sorted({r['id'] for r in self.v.roles()}))}"])
        evidence: list[str] = []
        # ---- 1. the server's own answer for a real user holding the role (authoritative)
        server_answer, user = None, None
        if self.server_permission is not None and self.soap:
            holders = self.v.role_holders(role)
            if holders:
                user = min(holders, key=lambda u: len(self.v.roles_of(u)))
                server_answer = self.server_permission(user, perm)
                others = [r for r in self.v.roles_of(user) if r != role]
                evidence.append(f"Server (SOAP Security.hasPermission) for user '{user}': "
                                f"{'granted' if server_answer else 'denied' if server_answer is False else 'no answer'}")
                if others:
                    evidence.append(f"Note: '{user}' also holds roles {', '.join(others)}; the answer reflects all of them.")
            else:
                evidence.append(f"No user holds role '{role}', so the server cannot be asked. "
                                "Assign the role to a test user to get a server-confirmed result.")
        # ---- 2. what the permission files say
        cfg = self._config_permission(role, perm)
        if cfg is not None:
            evidence.append(f"Configuration (permissions.xml): {cfg[0]}")
            evidence += cfg[1]
        if server_answer is not None:
            ok = server_answer == want
            act = f"server says {'granted' if server_answer else 'denied'} for '{user}'"
            if cfg is not None and (cfg[0].startswith("granted")) != server_answer:
                evidence.append("Warning: the permission files and the server disagree; the server answer is used.")
            return _result(tc, TestStatus.PASS if ok else TestStatus.FAIL, exp, act,
                           "SOAP Security.hasPermission", evidence)
        if cfg is not None:
            granted = cfg[0].startswith("granted")
            ok = granted == want
            return _result(tc, TestStatus.PARTIAL_PASS if ok else TestStatus.FAIL, exp, f"configuration: {cfg[0]}",
                           "permissions.xml", evidence + ["Not confirmed by the server (no SOAP login or no user in this role)."]
                           if ok else evidence)
        return _result(tc, TestStatus.BLOCKED, exp, "not checked: no permission data available",
                       "-", [self.v.why_missing("permissions"),
                             "Enable the SVN configuration reader, or sign in to SOAP and assign the role to a test user."])

    def _config_permission(self, role: str, perm: str) -> tuple[str, list[str]] | None:
        if not self.v.has_data("permissions", "rules"):
            return None
        applicable = {role} | {"user", "everyone"} if role not in SPECIAL_ROLES else {role}
        grants, denies = [], []
        for r in self.v.permission_rules():
            if r["role"] in applicable and r["permission"] == perm and not r["permission"].startswith("@"):
                (grants if r["effect"] == "grant" else denies).append(r)
        fmt = lambda r: f"{r['effect']} for role '{r['role']}' in {r['scope']} scope ({r['sources'][0]['location']})"  # noqa: E731
        lines = [fmt(r) for r in denies + grants]
        if denies:
            return "denied (an explicit deny exists)", lines
        if grants:
            via = "" if any(g["role"] == role for g in grants) else " (via the implicit 'user' role)"
            return f"granted{via}", lines
        return "not granted (no rule grants it to this role)", ["No grant found for the role or the implicit 'user'/'everyone' roles."]

    # ------------------------------------------------------------------ types, fields, enums
    def _type_exists(self, tc, spec):
        if not self.v.has_data("types", "types"):
            return self._blocked(tc, spec, "types", "Work Item types")
        name = spec.target["type"]
        tid = self.v.resolve_type(name)
        return self._present(tc, spec, tid is not None, f"Work Item type '{name}'", f"id '{tid}'" if tid else "",
                             "Work Item types", [f"Known types: {', '.join(t['id'] for t in self.v.types())}"])

    def _type_or_none(self, spec):
        name = spec.target.get("type")
        if not name:
            return None, None
        tid = self.v.resolve_type(name)
        return tid, name

    def _field_exists(self, tc, spec):
        if not (self.v.has_data("fields", "effective") or self.v.has_data("fields", "layers")):
            return self._blocked(tc, spec, "fields", "custom field definitions")
        tid, tname = self._type_or_none(spec)
        if tname and tid is None:
            return _result(tc, TestStatus.FAIL, spec.describe(), f"Work Item type '{tname}' does not exist",
                           "Work Item types", [f"Known types: {', '.join(t['id'] for t in self.v.types())}"])
        f = self.v.resolve_field(spec.target["field"], tid)
        name = spec.target["field"]
        evidence = []
        found = f is not None
        if f and spec.target.get("field_type"):
            want = norm(spec.target["field_type"])
            have = norm(f.get("type", ""))
            alias = {"number": "integer", "checkbox": "boolean", "datetime": "datetime", "dropdown": "enum",
                     "richtext": "texthtml"}
            want = alias.get(want, want)
            type_ok = have.startswith(want) or want in have or (want.startswith("enum") and have.startswith("enum"))
            evidence.append(f"Field type is '{f.get('type')}', requirement says '{spec.target['field_type']}'")
            if not type_ok:
                return _result(tc, TestStatus.FAIL, f"field '{name}' of type {spec.target['field_type']}",
                               f"field exists with type '{f.get('type')}'", "custom field definitions", evidence)
        where = (f"{'custom' if f['custom'] else 'built-in'} field '{f['id']}'" + (f" for {tid}" if tid else "")) if f else ""
        return self._present(tc, spec, found, f"Field '{name}'", where, "custom field definitions", evidence)

    def _field_required(self, tc, spec):
        if not (self.v.has_data("fields", "effective") or self.v.has_data("fields", "layers")):
            return self._blocked(tc, spec, "fields", "custom field definitions")
        tid, tname = self._type_or_none(spec)
        if tname and tid is None:
            return _result(tc, TestStatus.FAIL, spec.describe(), f"Work Item type '{tname}' does not exist", "Work Item types")
        name = spec.target["field"]
        f = self.v.resolve_field(name, tid)
        want = spec.expect == Expectation.REQUIRED
        if f is None:
            return _result(tc, TestStatus.FAIL, f"field '{name}' is {'mandatory' if want else 'optional'}",
                           f"field '{name}' does not exist" + (f" for {tid}" if tid else ""), "custom field definitions")
        # mandatory through the workflow (required fields of actions)
        by_wf = []
        for w in self.v.workflows():
            if w["prototype"] == "WorkItem" and (tid is None or w["target_type"] == tid):
                by_wf += [f"{w['target_type']}.{a['id']}" for a in w["actions"] if f["id"] in a["required_fields"]]
        exp = f"field '{f['id']}' is {'mandatory' if want else 'optional'}" + (f" for {tid}" if tid else "")
        if f["custom"] and f.get("required"):
            act, ok_hard = "custom field is marked required", True
        elif by_wf:
            act, ok_hard = "required only when running workflow actions: " + ", ".join(by_wf[:6]), False
        else:
            act, ok_hard = "not required", False
        evidence = [f"Field definition source: {f['sources'][0]['location']}"] if f.get("sources") else []
        if want:
            if ok_hard:
                return _result(tc, TestStatus.PASS, exp, act, "custom field definitions", evidence)
            if by_wf:
                return _result(tc, TestStatus.PARTIAL_PASS, exp, act, "workflow definitions",
                               evidence + ["Mandatory at those transitions, but a Work Item can be saved without it otherwise."])
            return _result(tc, TestStatus.FAIL, exp, act, "custom field definitions", evidence)
        return _result(tc, TestStatus.PASS if not (ok_hard or by_wf) else TestStatus.FAIL, exp, act,
                       "custom field definitions", evidence)

    def _field_read_only(self, tc, spec):
        rules = [r for r in self.v.items("field_rules", "rules") if r["kind"] == "read_only"]
        if not rules:
            return self._blocked(tc, spec, "field_rules", "read-only field rules")
        tid, _ = self._type_or_none(spec)
        f = self.v.resolve_field(spec.target["field"], tid)
        fid = f["id"] if f else spec.target["field"]
        hits = [r for r in rules if fid in r["fields"] and (tid is None or r.get("target_type") in (None, tid))]
        ev = [f"{r['ref']} ({r.get('target_type') or 'all types'}, {r['scope']}): {r['sources'][0]['location']}" for r in hits]
        ev.append("Read-only rules only affect the UI; the server does not enforce them.")
        return _result(tc, TestStatus.PASS if hits else TestStatus.FAIL, f"field '{fid}' is read-only",
                       f"read-only in {len(hits)} status rule(s)" if hits else "no read-only rule covers it",
                       "read-only field rules", ev)

    def _enum_contains(self, tc, spec):
        if not self.v.has_data("enumerations", "enumerations"):
            return self._blocked(tc, spec, "enumerations", "enumerations")
        tid, _ = self._type_or_none(spec)
        eid, how = self.v.resolve_enum(spec.target["enum"], tid)
        if eid is None:
            return _result(tc, TestStatus.FAIL, spec.describe(), f"enumeration '{spec.target['enum']}' not found",
                           "enumerations", ["Known: " + ", ".join(sorted({e['enum_id'] for e in self.v.enumerations()}))[:300]])
        opts = self.v.enum_options(eid, tid) or []
        have = {norm(o["id"]) for o in opts} | {norm(o.get("name")) for o in opts}
        wanted = spec.target["options"]
        missing = [w for w in wanted if norm(w) not in have]
        present = [w for w in wanted if norm(w) in have]
        ev = [how, "Options: " + ", ".join(f"{o['id']}" for o in opts)]
        if spec.expect == Expectation.ABSENT:
            ok = not present
            return _result(tc, TestStatus.PASS if ok else TestStatus.FAIL, f"'{eid}' does not contain {', '.join(wanted)}",
                           "none present" if ok else f"still present: {', '.join(present)}", "enumerations", ev)
        return _result(tc, TestStatus.PASS if not missing else TestStatus.FAIL, f"'{eid}' contains {', '.join(wanted)}",
                       "all present" if not missing else f"missing: {', '.join(missing)}", "enumerations", ev)

    # ------------------------------------------------------------------ workflow
    def _workflow_for(self, tc, spec):
        tid, tname = self._type_or_none(spec)
        if not self.v.has_data("workflow", "effective"):
            return None, None, self._blocked(tc, spec, "workflow", "workflow definitions")
        if tname and tid is None:
            return None, None, _result(tc, TestStatus.FAIL, spec.describe(), f"Work Item type '{tname}' does not exist",
                                       "Work Item types", [f"Known types: {', '.join(t['id'] for t in self.v.types())}"])
        wfs = [self.v.workflow(tid)] if tid else [w for w in self.v.workflows() if w["prototype"] == "WorkItem"]
        wfs = [w for w in wfs if w]
        if not wfs:
            return None, None, _result(tc, TestStatus.FAIL, spec.describe(), f"no workflow found for '{tid}'", "workflow definitions")
        return tid, wfs, None

    def _status_exists(self, tc, spec):
        tid, wfs, err = self._workflow_for(tc, spec)
        if err:
            return err
        sid = None
        for w in wfs:
            sid = self.v.resolve_status(spec.target["status"], w["target_type"])
            if sid:
                break
        known = sorted({o["id"] for w in wfs for o in self.v.statuses(w["target_type"])})
        return self._present(tc, spec, sid is not None, f"Status '{spec.target['status']}'" + (f" for {tid}" if tid else ""),
                             f"id '{sid}'" if sid else "", "workflow + status enumeration", [f"Known statuses: {', '.join(known)}"])

    def _transition(self, tc, spec):
        tid, wfs, err = self._workflow_for(tc, spec)
        if err:
            return err
        w = wfs[0]
        a = self.v.resolve_status(spec.target["from_status"], w["target_type"])
        b = self.v.resolve_status(spec.target["to_status"], w["target_type"])
        if a is None or b is None:
            miss = [n for n, x in ((spec.target["from_status"], a), (spec.target["to_status"], b)) if x is None]
            return _result(tc, TestStatus.FAIL, spec.describe(), f"unknown status: {', '.join(miss)}",
                           "workflow definitions", [f"Statuses of {w['target_type']}: {', '.join(w['statuses_list'])}"])
        hit = [t for t in w["transitions"] if t["from"] == a and t["to"] == b]
        allowed = bool(hit)
        want = spec.expect == Expectation.ALLOWED
        ev = [f"Workflow: {w['sources'][0]['location']}"]
        ev += [f"{t['from']} --{t['action']}--> {t['to']}" for t in hit]
        if not allowed:
            ev.append("Transitions from " + a + ": " + (", ".join(f"{t['action']} -> {t['to']}" for t in w["transitions"] if t["from"] == a) or "none"))
        return _result(tc, TestStatus.PASS if allowed == want else TestStatus.FAIL,
                       f"{w['target_type']}: {a} -> {b} is {'allowed' if want else 'blocked'}",
                       f"{'allowed via ' + hit[0]['action'] if allowed else 'no such transition'}", "workflow definitions", ev)

    def _action_field(self, tc, spec):
        tid, wfs, err = self._workflow_for(tc, spec)
        if err:
            return err
        w = wfs[0]
        an = norm(spec.target["action"])
        act = next((a for a in w["actions"] if an in (norm(a["id"]), norm(a.get("name"))) or norm(a["id"]).startswith(an[:5])), None)
        if act is None:
            return _result(tc, TestStatus.FAIL, spec.describe(), f"workflow of {w['target_type']} has no action '{spec.target['action']}'",
                           "workflow definitions", [f"Actions: {', '.join(a['id'] for a in w['actions'])}"])
        f = self.v.resolve_field(spec.target["field"], w["target_type"])
        fid = f["id"] if f else spec.target["field"]
        ok = fid in act["required_fields"]
        return _result(tc, TestStatus.PASS if ok else TestStatus.FAIL, f"action '{act['id']}' requires '{fid}'",
                       "required" if ok else f"required fields: {', '.join(act['required_fields']) or 'none'}",
                       "workflow definitions", [f"Workflow: {w['sources'][0]['location']}"])

    # ------------------------------------------------------------------ links, documents
    def _link_role(self, tc, spec):
        if not self.v.has_data("links", "link_roles"):
            return self._blocked(tc, spec, "links", "link roles")
        r = self.v.resolve_link_role(spec.target["link_role"])
        return self._present(tc, spec, r is not None, f"Link role '{spec.target['link_role']}'", f"id '{r['id']}'" if r else "",
                             "link roles", [f"Known link roles: {', '.join(x['id'] for x in self.v.link_roles())}"])

    def _document_type(self, tc, spec):
        if not self.v.has_data("documents", "document_types"):
            return self._blocked(tc, spec, "documents", "document types")
        t = self.v.resolve_document_type(spec.target["document_type"])
        return self._present(tc, spec, t is not None, f"Document type '{spec.target['document_type']}'",
                             f"id '{t}'" if t else "", "document types",
                             [f"Known: {', '.join(x['id'] for x in self.v.document_types())}"])

    def _traceability(self, tc, spec):
        if not self.v.has_data("links", "link_roles"):
            return self._blocked(tc, spec, "links", "link roles")
        a = self.v.resolve_type(spec.target["from_type"])
        b = self.v.resolve_type(spec.target["to_type"])
        missing = [n for n, x in ((spec.target["from_type"], a), (spec.target["to_type"], b)) if x is None]
        if missing:
            return _result(tc, TestStatus.FAIL, spec.describe(), f"unknown Work Item type: {', '.join(missing)}", "Work Item types")
        allowed = []
        for r in self.v.link_roles():
            rules = r.get("rules") or []
            if not rules or any(a in [x.strip() for x in f.split(",")] and b in [x.strip() for x in t.split(",")]
                                for f, t in rules):
                allowed.append(r["id"] + ("" if rules else " (no type restriction)"))
        if not allowed:
            return _result(tc, TestStatus.FAIL, f"{a} can be linked to {b}", "no link role allows this pair",
                           "link roles")
        if self.data is not None:
            covered, uncovered = coverage(self.data, a, b)
            total = len(covered) + len(uncovered)
            ev = [f"Read {len(self.data.items)} Work Items via {self.data.source}"
                  + (" (limit reached: only these were checked)" if self.data.truncated else ""),
                  f"Link roles that allow it: {', '.join(allowed[:6])}"]
            if total == 0:
                return _result(tc, TestStatus.PARTIAL_PASS, f"every {a} is linked to a {b}",
                               f"there are no '{a}' Work Items in this project yet", "Work Items", ev)
            pct = round(100 * len(covered) / total, 1)
            ev += [f"Not linked: {i.id}" for i in uncovered[:25]] + ([f"... and {len(uncovered) - 25} more"] if len(uncovered) > 25 else [])
            ok = not uncovered
            st = TestStatus.PASS if ok and not self.data.truncated else TestStatus.PARTIAL_PASS if ok else TestStatus.FAIL
            return _result(tc, st, f"every {a} is linked to a {b}",
                           f"{len(covered)} of {total} {a} item(s) are linked to a {b} ({pct}% coverage)", f"Work Items ({self.data.source})", ev)
        return _result(tc, TestStatus.PARTIAL_PASS, f"every {a} is linked to a {b}",
                       f"configuration allows linking via: {', '.join(allowed[:6])}", "link roles",
                       ["Whether every existing item is actually linked is not measured by this check."])
