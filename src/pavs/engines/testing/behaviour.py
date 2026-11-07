"""Behavioural tests: really try things in Polarion, but ONLY in an authorized test project.

Safety rules (each is enforced in code and covered by tests):
- every write passes the Write Guard (authorized project + run id, never production without typed confirmation);
- every Work Item created is recorded in the artifact table at once, titled "[PAVS-<run>] ...";
- before deleting anything the item is read back and deleted only if its title still carries our tag;
- at most `max_items` items are created per run; cleanup runs even when a check crashes.
Behavioural tests use the REST API. Without REST (e.g. a server where it is off) they are BLOCKED, never faked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.enums import TestStatus
from ...domain.errors import PavsError, WriteNotAuthorized
from ...domain.testing import TestArtifact
from ...infra.storage import ArtifactRepository
from ...polarion.rest.client import RestClient
from ...polarion.safety import WriteAuthorization
from .view import ConfigView

ENDPOINT_DENIED = "necessary permissions to perform this operation"     # REST endpoint permission, not item permission


@dataclass
class Account:
    role: str
    user: str
    token: str


@dataclass
class BResult:
    status: TestStatus
    actual: str
    evidence: list[str] = field(default_factory=list)
    mechanism: str = "REST (behavioural test)"


def classify(resp) -> tuple[str, str]:
    """('granted'|'denied'|'endpoint'|'auth'|'other', detail) for a REST response."""
    detail = ""
    try:
        err = (resp.json().get("errors") or [{}])[0]
        detail = err.get("detail", "") or err.get("title", "")
    except Exception:
        detail = resp.reason_phrase
    if 200 <= resp.status_code < 300:
        return "granted", ""
    if resp.status_code == 401:
        return "auth", detail
    if resp.status_code == 403:
        return ("endpoint" if ENDPOINT_DENIED in detail else "denied"), detail
    return "other", f"HTTP {resp.status_code}: {detail}"


class Behaviour:
    def __init__(self, *, session, project: str, run_id: str, env_id: str, auth: WriteAuthorization, view: ConfigView,
                 accounts: dict[str, Account], artifacts: ArtifactRepository, max_items: int = 30) -> None:
        self.s = session
        self.project = project
        self.run_id = run_id
        self.env_id = env_id
        self.auth = auth
        self.view = view
        self.accounts = accounts
        self.artifacts = artifacts
        self.max_items = max_items
        self.prefix = f"[PAVS-{run_id}]"
        self.created: list[TestArtifact] = []
        self.admin: RestClient | None = session.rest
        self._clients: dict[str, RestClient] = {}

    # ------------------------------------------------------------------ availability
    def available(self) -> tuple[bool, str]:
        if self.admin is None:
            return False, ("Behavioural tests use the REST API, which is not signed in for this environment "
                           "(needs a Personal Access Token and REST enabled on the server).")
        return True, ""

    def client_for(self, account: Account) -> RestClient:
        if account.user not in self._clients:
            self._clients[account.user] = RestClient(self.s.transport, self.s.environment.base_url, account.token)
        return self._clients[account.user]

    # ------------------------------------------------------------------ primitive REST calls (all writes carry the authorization)
    def _send(self, client: RestClient, method: str, path: str, body=None, params=None):
        return client.send(method, path, json=body, params=params, write_auth=self.auth)

    def _path(self, ref: str = "") -> str:
        return f"/projects/{self.project}/workitems" + (f"/{ref}" if ref else "")

    def create_item(self, client: RestClient, wi_type: str, tail: str, extra: dict | None = None) -> tuple[str | None, str]:
        if len(self.created) >= self.max_items:
            return None, f"limit of {self.max_items} test Work Items per run reached"
        title = f"{self.prefix} {tail}"
        body = {"data": [{"type": "workitems", "attributes": {"type": wi_type, "title": title, **(extra or {})}}]}
        resp = self._send(client, "POST", self._path(), body)
        kind, detail = classify(resp)
        if kind != "granted":
            return None, f"{kind}: {detail}" if detail else kind
        try:
            ref = resp.json()["data"][0]["id"].split("/")[-1]
        except Exception:
            return None, "the server created something but returned no id"
        art = TestArtifact(run_id=self.run_id, environment_id=self.env_id, project_id=self.project, ref=ref, title=title)
        self.artifacts.add(art)                  # recorded immediately so a crash can still be cleaned up
        self.created.append(art)
        return ref, ""

    def get_item(self, client: RestClient, ref: str):
        return self._send(client, "GET", self._path(ref), params={"fields[workitems]": "title,status,type"})

    # ------------------------------------------------------------------ choosing a type to test with
    def test_type(self) -> str | None:
        types = [t["id"] for t in self.view.types()]
        for t in ("task", "issue", "defect"):
            if t in types:
                return t
        return types[0] if types else None

    # ------------------------------------------------------------------ workflow transition (allowed)
    def transition(self, wi_type: str, from_status: str, to_status: str, action_id: str,
                   required_fields: list[str], user_id: str | None) -> BResult:
        ok, why = self.available()
        if not ok:
            return BResult(TestStatus.BLOCKED, "not run", [why])
        ref, err = self.create_item(self.admin, wi_type, f"{wi_type} {from_status}->{to_status}", {"status": from_status})
        if ref is None:
            return BResult(TestStatus.BLOCKED, "could not create a test Work Item",
                           [f"Creating a '{wi_type}' in status '{from_status}' failed: {err}",
                            "The test needs a Work Item in the starting status; fix the cause or test this by hand."])
        ev = [f"Created test Work Item {ref} in project {self.project} (status '{from_status}')"]
        acts = self._send(self.admin, "GET", self._path(ref) + "/actions/getWorkflowActions")
        if acts.status_code != 200:
            return BResult(TestStatus.BLOCKED, "could not read the available workflow actions", ev + [classify(acts)[1]])
        listing = acts.json().get("data") or []
        mine = next((a for a in listing if a.get("nativeActionId") == action_id or a.get("id") == action_id), None)
        if mine is None:
            return BResult(TestStatus.FAIL, f"action '{action_id}' is not offered for a '{wi_type}' in '{from_status}'",
                           ev + ["Actions offered: " + ", ".join(a.get("nativeActionId", "?") for a in listing)])
        if not mine.get("available", True):
            return BResult(TestStatus.FAIL, f"action '{action_id}' is not available: {mine.get('unavailableReason', 'no reason given')}",
                           ev + ["The configuration lists this transition, but the server refuses it for a fresh item. "
                                 "A condition or a missing required field is in the way."])
        attrs, rels, missing = self._fill_required(required_fields, wi_type, user_id)
        if missing:
            return BResult(TestStatus.BLOCKED, "the action needs fields this tool cannot fill automatically",
                           ev + [f"Fill by hand and retest: {', '.join(missing)}"])
        body = {"data": {"type": "workitems", "id": f"{self.project}/{ref}", "attributes": attrs, "relationships": rels}}
        resp = self._send(self.admin, "PATCH", self._path(ref), body, params={"workflowAction": action_id})
        kind, detail = classify(resp)
        if kind != "granted":
            return BResult(TestStatus.FAIL if kind in ("other", "denied") else TestStatus.BLOCKED,
                           f"the server rejected action '{action_id}'", ev + [detail or kind])
        after = self.get_item(self.admin, ref)
        status = ((after.json().get("data") or {}).get("attributes") or {}).get("status") if after.status_code == 200 else None
        ev.append(f"Ran action '{action_id}'; the Work Item is now in status '{status}'")
        if status == to_status:
            return BResult(TestStatus.PASS, f"{from_status} -> {to_status} worked on a real Work Item ({ref})", ev)
        return BResult(TestStatus.FAIL, f"the status after the action is '{status}', expected '{to_status}'", ev)

    def _fill_required(self, fields: list[str], wi_type: str, user_id: str | None) -> tuple[dict, dict, list[str]]:
        attrs: dict = {}
        rels: dict = {}
        missing: list[str] = []
        for f in fields:
            if f == "assignee":
                if user_id:
                    rels["assignee"] = {"data": [{"type": "users", "id": user_id}]}
                else:
                    missing.append("assignee (current user unknown)")
                continue
            info = self.view.resolve_field(f, wi_type)
            options = None
            if info and str(info.get("enum_id") or "").startswith("@user"):     # a user-picker field: use the current user
                if user_id:
                    one = {"type": "users", "id": user_id}
                    rels[f] = {"data": [one] if info.get("multi") else one}
                else:
                    missing.append(f"{f} (current user unknown)")
                continue
            if info and info.get("enum_id") and not str(info["enum_id"]).startswith("@"):
                options = self.view.enum_options(info["enum_id"], wi_type)
            elif f in ("resolution", "severity", "priority"):
                options = self.view.enum_options(f, wi_type)
            if options:
                pick = next((o for o in options if o.get("default")), options[0])
                attrs[f] = pick["id"]
            elif info and info.get("custom") and not info.get("enum_id"):
                t = (info.get("type") or "string").lower()
                attrs[f] = (1 if "int" in t else True if "bool" in t else 1.0 if "float" in t else "PAVS test")
            else:
                missing.append(f)
        return attrs, rels, missing

    # ------------------------------------------------------------------ permission, as a real test account
    SUPPORTED = {("WorkItem", "read"), ("WorkItem", "modify"), ("WorkItem", "delete"), ("WorkItem", "create"),
                 ("Comment", "create")}

    def permission_as(self, account: Account, obj: str, action: str, expect_granted: bool) -> BResult | None:
        if (obj, action) not in self.SUPPORTED:
            return None
        ok, why = self.available()
        if not ok:
            return BResult(TestStatus.BLOCKED, "not run", [why])
        wi_type = self.test_type()
        if wi_type is None:
            return BResult(TestStatus.BLOCKED, "no Work Item type is known", ["Inspect the project first."])
        client = self.client_for(account)
        ev: list[str] = []

        if (obj, action) == ("WorkItem", "create"):
            ref, err = self.create_item(client, wi_type, f"created by {account.user}")
            return self._judge(expect_granted, "granted" if ref else err.split(":")[0] if err else "other",
                               err, account, ev + ([f"Work Item {ref} was created by '{account.user}'"] if ref else []))

        ref, err = self.create_item(self.admin, wi_type, f"{action} test for {account.user}")
        if ref is None:
            return BResult(TestStatus.BLOCKED, "could not create the test Work Item", [f"Creating a '{wi_type}' failed: {err}"])
        ev.append(f"Test Work Item {ref} created by the admin account; the check runs as '{account.user}' ({account.role})")
        control = classify(self.get_item(client, ref))
        if control[0] in ("endpoint", "auth"):
            reason = ("the token was rejected" if control[0] == "auth" else
                      "the account has no REST permission for GET (REST endpoint permissions in Administration)")
            return BResult(TestStatus.BLOCKED, f"'{account.user}' cannot use the REST API", ev + [reason])
        if control[0] == "denied" and action != "read":
            ev.append("Note: the account cannot read the item either, so a denial may come from missing read access.")

        if (obj, action) == ("WorkItem", "read"):
            kind, detail = control
        elif (obj, action) == ("WorkItem", "modify"):
            kind, detail = classify(self._send(client, "PATCH", self._path(ref), {"data": {
                "type": "workitems", "id": f"{self.project}/{ref}", "attributes": {"title": f"{self.prefix} edited by {account.user}"}}}))
        elif (obj, action) == ("WorkItem", "delete"):
            resp = self._send(client, "DELETE", self._path(), {"data": [{"type": "workitems", "id": f"{self.project}/{ref}"}]})
            kind, detail = classify(resp)
            if kind == "granted":
                self._forget(ref)
        else:   # Comment.create
            kind, detail = classify(self._send(client, "POST", self._path(ref) + "/comments", {"data": [{
                "type": "workitem_comments", "attributes": {"text": {"type": "text/plain", "value": f"{self.prefix} test comment"}}}]}))
        return self._judge(expect_granted, kind, detail, account, ev)

    def _judge(self, expect_granted: bool, kind: str, detail: str, account: Account, ev: list[str]) -> BResult:
        if kind == "endpoint":
            return BResult(TestStatus.BLOCKED, f"'{account.user}' lacks the REST endpoint permission for this verb",
                           ev + [detail, "This says nothing about the Work Item permission itself. Grant the REST "
                                         "endpoint permission to the test role (Administration > Roles) and retest."])
        if kind == "auth":
            return BResult(TestStatus.BLOCKED, "the test account's token was rejected", ev + [detail])
        if kind == "other":
            return BResult(TestStatus.ERROR, "unexpected answer from the server", ev + [detail])
        granted = kind == "granted"
        ev.append(f"Server answer for '{account.user}': {'allowed' if granted else 'refused'}" + (f" ({detail})" if detail and not granted else ""))
        if granted == expect_granted:
            return BResult(TestStatus.PASS, f"really {'allowed' if granted else 'refused'} for '{account.user}'", ev)
        return BResult(TestStatus.FAIL, f"really {'allowed' if granted else 'refused'} for '{account.user}', "
                                        f"but the requirement says {'allowed' if expect_granted else 'refused'}", ev)

    def _forget(self, ref: str) -> None:
        for a in self.created:
            if a.ref == ref and a.deleted_at is None:
                self.artifacts.mark_deleted(a.id)
                from ...domain.models import utcnow
                a.deleted_at = utcnow()

    # ------------------------------------------------------------------ cleanup (always runs)
    def cleanup(self) -> tuple[int, list[str], list[str]]:
        """(deleted, leftovers, log). Deletes only items that still carry our tag in their title."""
        return cleanup_artifacts(self.s, self.artifacts, self.auth, self.project, self.env_id,
                                 [a for a in self.created if a.deleted_at is None])


def cleanup_artifacts(session, artifacts: ArtifactRepository, auth: WriteAuthorization, project: str, env_id: str,
                      items: list[TestArtifact]) -> tuple[int, list[str], list[str]]:
    admin = session.rest
    log: list[str] = []
    if not items:
        return 0, [], log
    if admin is None:
        return 0, [a.ref for a in items], ["REST is not signed in, so the test Work Items could not be deleted. "
                                           "Reconnect with a Personal Access Token and use 'Clean up test data'."]
    safe: list[TestArtifact] = []
    resolved: set[str] = set()                 # refs that no longer exist in Polarion
    for a in items:
        resp = admin.send("GET", f"/projects/{project}/workitems/{a.ref}", params={"fields[workitems]": "title"})
        if resp.status_code == 404:
            artifacts.mark_deleted(a.id)
            resolved.add(a.ref)
            log.append(f"{a.ref}: already gone")
            continue
        if resp.status_code != 200:
            log.append(f"{a.ref}: could not be read ({resp.status_code}); left in place")
            continue
        title = ((resp.json().get("data") or {}).get("attributes") or {}).get("title", "")
        if not title.startswith(f"[PAVS-{a.run_id}]"):
            log.append(f"{a.ref}: title no longer carries the test tag, so it was NOT deleted")
            continue
        safe.append(a)
    if safe:
        try:
            resp = admin.send("DELETE", f"/projects/{project}/workitems", json={
                "data": [{"type": "workitems", "id": f"{project}/{a.ref}"} for a in safe]}, write_auth=auth)
        except (WriteNotAuthorized, PavsError) as exc:
            log.append(f"delete refused: {exc}")
        else:
            if 200 <= resp.status_code < 300:
                for a in safe:
                    artifacts.mark_deleted(a.id)
                    resolved.add(a.ref)
            else:
                log.append(f"delete failed: {classify(resp)[1]}")
    leftovers = sorted(a.ref for a in items if a.ref not in resolved)
    return len(resolved) - sum(1 for l in log if l.endswith("already gone")), leftovers, log
