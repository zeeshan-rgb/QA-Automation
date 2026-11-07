"""In-memory Work Items for the mock: REST create / read / change / workflow action / delete, with
per-user item permissions taken from the same fake configuration the inspection reads.

Behaviour copied from the REST guide: POST -> 201 + ids; PATCH / DELETE -> 204 without a body;
`PATCH ...?workflowAction=<id>` runs a workflow action; 403 "Sorry, you do not have the necessary
permissions..." is the REST ENDPOINT permission, other 403 texts are ITEM permissions.
"""
from __future__ import annotations

from . import config_api as C

PERM = "com.polarion.persistence.object."
PREFIX = {"acme_demo": "AD", "acme_test": "AT"}

SEED = {
    "acme_demo": [
        {"id": "AD-1", "type": "requirement", "status": "draft", "title": "Login", "links": []},
        {"id": "AD-2", "type": "task", "status": "open", "title": "Write docs", "links": [("relates_to", "AD-1")]},
        {"id": "AD-3", "type": "testcase", "status": "open", "title": "Test login", "links": [("verifies", "AD-1")]},
        {"id": "AD-4", "type": "requirement", "status": "approved", "title": "Logout", "links": []},
        {"id": "AD-5", "type": "task", "status": "open", "title": "Spare", "links": []},
    ],
    "acme_test": [],
}


def err(code: int, title: str, detail: str):
    return code, {"errors": [{"status": str(code), "title": title, "detail": detail}]}


class WorkItems:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, dict]] = {p: {i["id"]: dict(i, links=list(i["links"]), comments=[]) for i in lst}
                                                  for p, lst in SEED.items()}
        self.counter = 100
        self.writes: list[tuple[str, str, str, str]] = []       # (user, method, project, ref)
        self.refuse_actions: set[str] = set()                   # test hook: workflow actions the server refuses

    # ------------------------------------------------------------------ helpers
    def can(self, user, project: str, suffix: str) -> bool:
        if "admin" in user.global_roles:
            return True
        return C.has_permission(project, user.id, PERM + suffix)

    def first_per_type(self, project: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for it in sorted(self.items.get(project, {}).values(), key=lambda i: i["id"]):
            out.setdefault(it["type"], it)
        return out

    def find(self, ref: str) -> tuple[str, dict] | None:
        for p, d in self.items.items():
            if ref in d:
                return p, d[ref]
        return None

    def _log(self, user, method, project, ref) -> None:
        self.writes.append((user.id, method, project, ref))

    # ------------------------------------------------------------------ REST
    def get(self, project: str, ref: str, user):
        it = self.items.get(project, {}).get(ref)
        if it is None:
            return err(404, "Not Found", f"Work Item '{ref}' not found.")
        if not self.can(user, project, "WorkItem.read"):
            return err(403, "Forbidden", f"You do not have permission to read Work Item '{ref}'.")
        return 200, {"data": {"type": "workitems", "id": f"{project}/{ref}", "attributes": {
            "id": ref, "title": it["title"], "status": it["status"], "type": it["type"]}}}

    def post(self, project: str, body: dict, user):
        if project not in self.items:
            return err(404, "Not Found", f"Project '{project}' not found.")
        out = []
        types = {o.id for o in (C.enum_options(project, "workitem-type") or [])}
        for entry in (body or {}).get("data") or []:
            a = entry.get("attributes") or {}
            if not self.can(user, project, "WorkItem.create"):
                return err(403, "Forbidden", f"You do not have permission to create Work Items in project '{project}'.")
            if a.get("type") not in types:
                return err(400, "Bad Request", f"Unknown Work Item type '{a.get('type')}'.")
            wf = C.workflow(project, a["type"])
            status = a.get("status") or (wf.initial_status if wf else "open")
            if wf and status not in wf.statuses:
                return err(400, "Bad Request", f"Status '{status}' is not defined for type '{a['type']}'.")
            self.counter += 1
            ref = f"{PREFIX.get(project, 'X')}-{self.counter}"
            self.items[project][ref] = {"id": ref, "type": a["type"], "status": status, "title": a.get("title", ""),
                                        "links": [], "comments": []}
            self._log(user, "POST", project, ref)
            out.append({"type": "workitems", "id": f"{project}/{ref}"})
        return 201, {"data": out}

    def patch(self, project: str, ref: str, body: dict, query: dict, user):
        it = self.items.get(project, {}).get(ref)
        if it is None:
            return err(404, "Not Found", f"Work Item '{ref}' not found.")
        if not self.can(user, project, "WorkItem.modify"):
            return err(403, "Forbidden", f"You do not have permission to modify Work Item '{ref}'.")
        data = (body or {}).get("data") or {}
        attrs, rels = data.get("attributes") or {}, data.get("relationships") or {}
        action = (query.get("workflowAction") or [None])[0]
        if action:
            wf = C.workflow(project, it["type"])
            tr = next((t for t in (wf.transitions if wf else []) if t.action == action and t.source == it["status"]), None)
            if tr is None:
                return err(400, "Bad Request", f"The workflow action '{action}' was not found.")
            if action in self.refuse_actions:
                return err(400, "Bad Request", f"'{action}' failed for Work Item '{ref}': a workflow condition is not met.")
            spec = wf.action(action)
            for f in (spec.required_fields if spec else []):
                if f not in attrs and f not in rels and not it.get(f):
                    return err(400, "Bad Request", f"'{spec.name}' failed for Work Item '{ref}': "
                                                   f"The required field '{f}' of Work Item '{ref}' is empty.")
            it["status"] = tr.target
        for k, v in attrs.items():
            if k != "status":
                it[k] = v
        self._log(user, "PATCH", project, ref)
        return 204, None

    def delete(self, project: str, body: dict, user):
        refs = [(e.get("id") or "").split("/")[-1] for e in (body or {}).get("data") or []]
        for ref in refs:
            if ref not in self.items.get(project, {}):
                return err(404, "Not Found", f"Work Item '{ref}' not found.")
            if not self.can(user, project, "WorkItem.delete"):
                return err(403, "Forbidden", f"You do not have permission to delete Work Item '{ref}'.")
        for ref in refs:
            del self.items[project][ref]
            self._log(user, "DELETE", project, ref)
        return 204, None

    def comment(self, project: str, ref: str, body: dict, user):
        it = self.items.get(project, {}).get(ref)
        if it is None:
            return err(404, "Not Found", f"Work Item '{ref}' not found.")
        if not self.can(user, project, "Comment.create"):
            return err(403, "Forbidden", f"You do not have permission to comment on Work Item '{ref}'.")
        it["comments"].append((body or {}).get("data"))
        self._log(user, "POST", project, ref)
        return 201, {"data": [{"type": "workitem_comments", "id": f"{project}/{ref}/1"}]}

    # ------------------------------------------------------------------ list view used by data checks
    def rest_list(self, project: str, query: dict) -> list[dict]:
        q = (query.get("query") or [""])[0]
        t = q.split("type:", 1)[1].split()[0] if "type:" in q else None
        out = []
        for it in sorted(self.items.get(project, {}).values(), key=lambda i: i["id"]):
            if t and it["type"] != t:
                continue
            out.append({"type": "workitems", "id": f"{project}/{it['id']}",
                        "attributes": {"id": it["id"], "status": it["status"], "type": it["type"], "title": it["title"]},
                        "relationships": {"linkedWorkItems": {"data": [
                            {"type": "linkedworkitems", "id": f"{project}/{it['id']}/{role}/{project}/{tgt}"}
                            for role, tgt in it["links"]]}}})
        return out
