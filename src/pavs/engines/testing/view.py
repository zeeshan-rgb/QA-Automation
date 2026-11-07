"""Read-only lookups over a ConfigSnapshot, used by health checks and requirement checks."""
from __future__ import annotations

import re
from typing import Any

from ...domain.config import ConfigSnapshot, SectionResult

BUILTIN_FIELDS = {
    "id": "ID", "title": "Title", "description": "Description", "status": "Status", "type": "Type",
    "severity": "Severity", "priority": "Priority", "resolution": "Resolution", "assignee": "Assignee",
    "author": "Author", "categories": "Categories", "created": "Created", "updated": "Updated",
    "dueDate": "Due Date", "initialEstimate": "Initial Estimate", "remainingEstimate": "Remaining Estimate",
    "timeSpent": "Time Spent", "plannedStart": "Planned Start", "plannedEnd": "Planned End",
    "linkedWorkItems": "Linked Work Items", "attachments": "Attachments", "comments": "Comments",
    "hyperlinks": "Hyperlinks", "approvals": "Approvals", "watches": "Watches", "votes": "Votes",
    "outlineNumber": "Outline Number", "module": "Document", "timePoint": "Time Point",
    "previousStatus": "Previous Status", "resolvedOn": "Resolved On", "project": "Project",
    "location": "Location", "version": "Version", "plannedIn": "Planned In", "linkedRevisions": "Linked Revisions",
    "workRecords": "Work Records", "externallyLinkedWorkItems": "External Links", "backlinkedWorkItems": "Back Links", "reviewer": None,   # None: not built-in (placeholder removed below)
}
BUILTIN_FIELDS = {k: v for k, v in BUILTIN_FIELDS.items() if v}
SPECIAL_ROLES = {"everyone", "self", "peer", "user"}     # implicit roles Polarion applies without assignment
SPECIAL_STATES = {"_default", "_create", ""}


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def singular(s: str) -> str:
    n = norm(s)
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


class ConfigView:
    def __init__(self, snap: ConfigSnapshot) -> None:
        self.snap = snap

    # ------------------------------------------------------------------ sections
    def section(self, area: str) -> SectionResult | None:
        return self.snap.section(area)

    def items(self, area: str, name: str) -> list[dict[str, Any]]:
        sec = self.section(area)
        return list(sec.items.get(name, [])) if sec else []

    def has_data(self, area: str, name: str) -> bool:
        return bool(self.items(area, name))

    def why_missing(self, area: str) -> str:
        """Human text for a section that could not be read, including how to fix it."""
        sec = self.section(area)
        if sec is None:
            return f"The '{area}' section was not inspected."
        parts = [f"{g.item}: {g.reason}" + (f" ({g.remedy})" if g.remedy else "") for g in sec.gaps]
        parts += sec.errors
        return "; ".join(parts) or "no data returned"

    # ------------------------------------------------------------------ types & fields
    def types(self) -> list[dict]:
        return self.items("types", "types")

    def resolve_type(self, name: str | None) -> str | None:
        if not name:
            return None
        n, s = norm(name), singular(name)
        for t in self.types():
            if n in (norm(t["id"]), norm(t.get("name"))) or s in (norm(t["id"]), norm(t.get("name"))):
                return t["id"]
        return None

    def fields_for(self, wi_type: str | None) -> list[dict]:
        eff = self.items("fields", "effective")
        if wi_type and any(f.get("target_type") == wi_type for f in eff):
            return [f for f in eff if f.get("target_type") == wi_type]
        if not wi_type and eff:
            return eff
        layers = self.items("fields", "layers")
        return [f for f in layers if wi_type is None or f.get("target_type") in (None, wi_type)]

    def resolve_field(self, name: str, wi_type: str | None = None) -> dict | None:
        """{'id','name','custom':bool,'required','type',...} or None."""
        n = norm(name)
        for f in self.fields_for(wi_type):
            if n in (norm(f["id"]), norm(f.get("name"))):
                return {**f, "custom": True}
        if wi_type is None:   # any type
            for f in self.items("fields", "effective") + self.items("fields", "layers"):
                if n in (norm(f["id"]), norm(f.get("name"))):
                    return {**f, "custom": True}
        for fid, fname in BUILTIN_FIELDS.items():
            if n in (norm(fid), norm(fname)):
                return {"id": fid, "name": fname, "custom": False, "required": False, "type": "built-in"}
        return None

    def field_ids(self, wi_type: str | None) -> set[str]:
        return {f["id"] for f in self.fields_for(wi_type)} | set(BUILTIN_FIELDS)

    # ------------------------------------------------------------------ enumerations & statuses
    def enumerations(self) -> list[dict]:
        return self.items("enumerations", "enumerations")

    def enum_options(self, enum_id: str, wi_type: str | None = None) -> list[dict] | None:
        cands = [e for e in self.enumerations() if e["enum_id"] == enum_id]
        if wi_type:
            typed = [e for e in cands if e.get("target_type") == wi_type]
            if typed:
                return typed[0]["options"]
        generic = [e for e in cands if not e.get("target_type")]
        if generic:
            return generic[0]["options"]
        return cands[0]["options"] if cands else None

    def resolve_enum(self, name: str, wi_type: str | None = None) -> tuple[str | None, str]:
        """(enum id, how it was found) for an enumeration or a field name."""
        n = norm(name)
        for e in self.enumerations():
            last = e["enum_id"].rsplit("/", 1)[-1]
            if n in (norm(e["enum_id"]), norm(last)):
                return e["enum_id"], f"enumeration {e['enum_id']}"
        f = self.resolve_field(name, wi_type)
        if f and f.get("enum_id") and not str(f["enum_id"]).startswith("@"):
            return f["enum_id"], f"field {f['id']} uses enumeration {f['enum_id']}"
        if f and f["id"] in ("status", "severity", "priority", "resolution"):
            return f["id"], f"built-in field {f['id']}"
        return None, ""

    def statuses(self, wi_type: str | None) -> list[dict]:
        wf = self.workflow(wi_type)
        opts = self.enum_options("status", wi_type) or []
        out = {o["id"]: o for o in opts}
        if wf:
            for s in wf["statuses_list"]:
                out.setdefault(s, {"id": s, "name": s})
        return list(out.values())

    def resolve_status(self, name: str, wi_type: str | None) -> str | None:
        n = norm(name)
        for o in self.statuses(wi_type):
            if n in (norm(o["id"]), norm(o.get("name"))):
                return o["id"]
        return None

    # ------------------------------------------------------------------ workflow
    def workflows(self) -> list[dict]:
        out = []
        for w in self.items("workflow", "effective"):
            w = dict(w)
            seen: list[str] = []
            for s in [w.get("initial_status")] + [t["from"] if "from" in t else t.get("source") for t in w["transitions"]] \
                    + [t["to"] if "to" in t else t.get("target") for t in w["transitions"]]:
                if s and s not in seen:
                    seen.append(s)
            w["statuses_list"] = seen
            for t in w["transitions"]:
                t.setdefault("from", t.get("source"))
                t.setdefault("to", t.get("target"))
            out.append(w)
        return out

    def workflow(self, wi_type: str | None, prototype: str = "WorkItem") -> dict | None:
        for w in self.workflows():
            if w["prototype"] == prototype and w.get("target_type") == wi_type:
                return w
        return None

    # ------------------------------------------------------------------ roles & permissions
    def roles(self) -> list[dict]:
        return self.items("roles", "roles")

    def resolve_role(self, name: str) -> str | None:
        n = norm(name)
        for r in self.roles():
            if n == norm(r["id"]):
                return r["id"]
        for sp in SPECIAL_ROLES:
            if n == norm(sp):
                return sp
        return None

    def role_holders(self, role: str) -> list[str]:
        return sorted({a["principal"] for a in self.items("roles", "assignments")
                       if a["role"] == role and a.get("principal_kind", "user") == "user"})

    def roles_of(self, user: str) -> list[str]:
        return sorted({a["role"] for a in self.items("roles", "assignments") if a["principal"] == user})

    def permission_rules(self) -> list[dict]:
        return self.items("permissions", "rules")

    def customset_ids(self) -> set[str]:
        return {c["id"] for c in self.items("permissions", "customsets")}

    # ------------------------------------------------------------------ links, documents
    def link_roles(self) -> list[dict]:
        return self.items("links", "link_roles")

    def resolve_link_role(self, name: str) -> dict | None:
        n = norm(name)
        for r in self.link_roles():
            if n in (norm(r["id"]), norm(r.get("name")), norm(r.get("opposite_name"))):
                return r
        return None

    def document_types(self) -> list[dict]:
        return self.items("documents", "document_types")

    def resolve_document_type(self, name: str) -> str | None:
        n = norm(name)
        for t in self.document_types():
            if n in (norm(t["id"]), norm(t.get("name"))):
                return t["id"]
        return None
