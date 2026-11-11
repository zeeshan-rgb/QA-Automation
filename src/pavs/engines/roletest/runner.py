"""Drives the role test: an admin session prepares and removes test data, a session of the test user does the real work.

Every object created is titled "[PAVS-ROLE-<run>] ..." and recorded in a ledger BEFORE the next step, so a crash still
leaves a trail for the cleanup. Cleanup runs in `finally` and deletes only objects whose title still carries the tag.
All writes go through the Write Guard (the actors carry the run's authorization).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ...domain.errors import PavsError, SoapFault
from ...polarion.safety import WriteAuthorization
from ...polarion.soap.client import SoapClient, as_list
from .model import Cause, Outcome, Probe, classify

Progress = Callable[[str, int], None]

TYPE_ACTIONS = ("READ", "CREATE NEW", "MODIFY", "MODIFY FIELD (description)", "ADD LINK", "DELETE", "COMMENT",
                "RESOLVE COMMENTS", "APPROVE/DISAPPROVE", "CHANGE STATUS (workflow)")
DOCUMENT_ACTIONS = ("READ", "CREATE NEW", "MODIFY FIELDS", "MODIFY CONTENT", "DELETE", "COMMENT")

# Not drivable through the SOAP API: asked of the server's own permission check for this user (project-wide, so indicative).
ORACLE = [("DOCUMENT", "MANAGE", "persistence.object.Module.manageDocument"), ("DOCUMENT", "SIGN/DECLINE", "persistence.object.Module.sign"),
          ("DOCUMENT", "MANAGE SIGNATURES", "persistence.object.Module.manageSignatures"),
          ("TEST RUN", "READ", "persistence.object.TestRun.read"), ("TEST RUN", "CREATE NEW", "persistence.object.TestRun.create"),
          ("TEST RUN", "MODIFY", "persistence.object.TestRun.modify"), ("TEST RUN", "DELETE", "persistence.object.TestRun.delete"),
          ("TEST RUN", "EXECUTE", "persistence.object.TestRun.execute"),
          ("TEST RUN", "DEFINE TEST PARAMETERS", "testing.TestRun.defineParameters"),
          ("TEST RUN", "ADD TO PARAMETERS LIBRARY", "testing.TestRun.addParameterToLibrary"),
          ("TEST RUN", "COMMENT", "persistence.object.TestRunComment.create"),
          ("TEST RUN", "RESOLVE COMMENTS", "persistence.object.TestRunComment.key.resolved.modify"),
          ("COLLECTION", "READ", "persistence.object.BaselineCollection.read"), ("COLLECTION", "CREATE NEW", "persistence.object.BaselineCollection.create"),
          ("COLLECTION", "MODIFY", "persistence.object.BaselineCollection.modify"), ("COLLECTION", "DELETE", "persistence.object.BaselineCollection.delete")]


@dataclass(frozen=True)
class TypeSpec:
    id: str                    # Work Item type id, e.g. systemrequirement
    name: str                  # as shown in Polarion, e.g. System Requirement

    @property
    def group(self) -> str:
        return self.name.upper()


class Soap(Protocol):
    def try_call(self, service: str, op: str, params: dict | None = None, write: bool = False) -> tuple[bool, object]: ...
    def relogin(self) -> None: ...


class Actor:
    """A signed-in SOAP client that never raises for a Polarion answer. Writes carry the run's authorization."""

    def __init__(self, soap: SoapClient, name: str, project: str, auth: WriteAuthorization | None) -> None:
        self.soap, self.name, self.project, self.auth = soap, name, project, auth

    def call(self, service: str, op: str, params=None, write: bool = False):
        kw = dict(write_auth=self.auth, project_id=self.project) if write else {}
        return self.soap.call(service, op, params or {}, **kw)

    def try_call(self, service: str, op: str, params=None, write: bool = False):
        for attempt in (0, 1):
            try:
                return True, self.call(service, op, params, write)
            except SoapFault as exc:
                if attempt == 0 and exc.message.strip(" .").lower() == "not authorized" and self._relogin():
                    continue                       # the server forgot the session before doing anything: sign in again, once
                return False, exc.message[:400]
            except PavsError as exc:
                return False, str(exc)[:400]
        return False, "not authorized"

    def _relogin(self) -> bool:
        try:
            self.relogin()
            return True
        except PavsError:
            return False

    def relogin(self) -> None:
        self.soap.relogin()


class Ledger(Protocol):
    def add(self, kind: str, uri: str, title: str) -> None: ...
    def open_items(self) -> list[tuple[str, str, str]]: ...          # (kind, uri, title)
    def done(self, uri: str) -> None: ...


class MemoryLedger:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def add(self, kind: str, uri: str, title: str) -> None:
        self.items.append((kind, uri, title))

    def open_items(self):
        return list(self.items)

    def done(self, uri: str) -> None:
        self.items = [i for i in self.items if i[1] != uri]


def uri_of(result) -> str | None:
    m = re.search(r"subterra:data-service:objects:/default/[^\"'\\ ]+", repr(result))
    return m.group(0) if m else None


def first_uri(result, marker: str) -> str | None:
    for m in re.finditer(r"subterra:data-service:objects:/default/[^\"'\\ ]+", repr(result)):
        if marker in m.group(0):
            return m.group(0)
    return None


@dataclass
class RunSetup:
    project: str
    user: str                         # the test user's id
    tag: str                          # "[PAVS-ROLE-<run>]"
    types: list[TypeSpec]
    space: str = "_default"           # document space used for the test documents
    areas: tuple[str, ...] = ("workitems", "documents", "other")
    roles_label: str = ""


class RoleTestRunner:
    def __init__(self, admin: Soap, user: Soap, setup: RunSetup, ledger: Ledger, progress: Progress | None = None) -> None:
        self.admin, self.user, self.s, self.ledger = admin, user, setup, ledger
        self.progress = progress or (lambda m, p: None)
        self.probes: list[Probe] = []
        self.notes: list[str] = []
        self.fixtures: dict[str, str] = {}              # type id -> fixture Work Item uri
        self.comments: dict[str, str] = {}              # type id -> fixture comment uri
        self.why_not: dict[str, tuple[Cause, str]] = {}   # type id -> why its fixture could not be created
        self.doc_uri: str | None = None
        self.project_uri = f"subterra:data-service:objects:/default/{setup.project}${{Project}}{setup.project}"
        self.user_uri = f"subterra:data-service:objects:/default/${{User}}{setup.user}"

    # ------------------------------------------------------------------ small helpers
    def _record(self, group: str, action: str, ok: bool, msg=None, mechanism: str | None = None) -> Probe:
        outcome, cause, detail = classify(ok, msg)
        p = Probe(group, action, outcome, cause, detail)
        if mechanism:
            p.mechanism = mechanism
        self.probes.append(p)
        return p

    def _blocked(self, group: str, action: str, why: str, cause: Cause = Cause.SETUP) -> None:
        self.probes.append(Probe(group, action, Outcome.BLOCKED, cause, why))

    def _missing(self, t: TypeSpec) -> tuple[str, Cause]:
        cause, why = self.why_not.get(t.id, (Cause.SETUP, "no test Work Item of this type could be prepared"))
        return why, cause

    def _item(self, type_id: str, title: str) -> dict:
        return {"t:project": {"@uri": self.project_uri}, "t:type": {"t:id": type_id}, "t:title": f"{self.s.tag} {title}",
                "t:description": {"ty:type": "text/plain", "ty:content": "created by the role test"}}

    @staticmethod
    def _text(content: str) -> dict:
        return {"ty:type": "text/plain", "ty:content": content}

    def _create_item(self, actor: Soap, type_id: str, title: str, note: str):
        ok, r = actor.try_call("Tracker", "createWorkItem", {"content": self._item(type_id, title)}, write=True)
        uri = uri_of(r) if ok else None
        if uri:
            self.ledger.add("workitem", uri, f"{self.s.tag} {title}")
        return ok, r, uri

    def _create_doc(self, actor: Soap, name: str, title: str, type_id: str):
        params = {"projectId": self.s.project, "location": self.s.space, "documentName": name, "documentTitle": f"{self.s.tag} {title}",
                  "allowedWITypes": [{"t:id": type_id}], "structureLinkRole": {"t:id": "parent"}, "homePageContent": "<p>role test</p>"}
        ok, r = actor.try_call("Tracker", "createDocument", params, write=True)
        if not ok and "structurelinkrole" in str(r).lower():
            params.pop("structureLinkRole")
            ok, r = actor.try_call("Tracker", "createDocument", params, write=True)
        uri = uri_of(r) if ok else None
        if uri:
            self.ledger.add("document", uri, f"{self.s.tag} {title}")
        return ok, r, uri

    # ------------------------------------------------------------------ the run
    def run(self) -> list[Probe]:
        n = max(1, len(self.s.types))
        try:
            if "workitems" in self.s.areas:
                for i, t in enumerate(self.s.types):                       # every fixture first: a Work Item needs another to link to
                    self.progress(f"Preparing test data for '{t.name}'", 2 + int(28 * i / n))
                    self._prepare_type(t)
                for i, t in enumerate(self.s.types):
                    self.progress(f"Testing '{t.name}'", 30 + int(55 * i / n))
                    self._test_type(t)
            if "documents" in self.s.areas:
                self.progress("Testing documents", 85)
                self._test_documents()
            if "other" in self.s.areas:
                self.progress("Asking the server about test runs and collections", 88)
                self._test_oracle()
        finally:
            self.progress("Removing the test data", 92)
            self.cleanup()
        self._explain_unreadable()
        self.progress("Done", 100)
        return self.probes

    def _explain_unreadable(self) -> None:
        """Without access to the project's repository nothing can be opened. When that was seen, an 'unreadable' READ is that, not a missing permission."""
        if not any(p.cause == Cause.REPO_ACCESS for p in self.probes):
            return
        for p in self.probes:
            if p.action == "READ" and p.outcome == Outcome.DENIED and "cannot be opened" in p.detail:
                p.outcome, p.cause = Outcome.BLOCKED, Cause.REPO_ACCESS
                p.detail = "the user has no access to the project's repository folder"

    # ------------------------------------------------------------------ Work Items
    def _prepare_type(self, t: TypeSpec) -> None:
        ok, r, uri = self._create_item(self.admin, t.id, f"fixture {t.id}", "fixture")
        if not uri:
            outcome, cause, detail = classify(ok, r)
            self.why_not[t.id] = (cause if cause != Cause.PERMISSION else Cause.SETUP, f"no test Work Item could be prepared: {detail or str(r)[:160]}")
            return
        self.fixtures[t.id] = uri
        self.admin.try_call("Tracker", "createComment", {"workitemURI": uri, "content": self._text("fixture comment for the resolve test")}, write=True)
        ok, item = self.admin.try_call("Tracker", "getWorkItemByUri", {"uri": uri})
        if ok:
            c = (item.get("getWorkItemByUriReturn") or {}).get("comments") if isinstance(item, dict) else None
            c = c.get("Comment") if isinstance(c, dict) else c
            c = c if isinstance(c, list) else [c] if c else []
            uris = [x.get("uri") for x in c if isinstance(x, dict) and x.get("uri")]
            if uris:
                self.comments[t.id] = uris[0]
        self.admin.try_call("Tracker", "addApprovee", {"workitemURI": uri, "approveeId": self.s.user}, write=True)

    def _test_type(self, t: TypeSpec) -> None:
        g, u = t.group, self.fixtures.get(t.id)
        # READ
        if u:
            ok, r = self.user.try_call("Tracker", "getWorkItemByUri", {"uri": u})
            body = r.get("getWorkItemByUriReturn", {}) if ok and isinstance(r, dict) else {}
            if ok and str(body.get("unresolvable")) == "false":
                self.probes.append(Probe(g, "READ", Outcome.ALLOWED))
            else:
                self.probes.append(self._unreadable(g, ok, r))
        else:
            self._blocked(g, "READ", *self._missing(t))
        # CREATE NEW (the user creates one)
        ok, r, uri = self._create_item(self.user, t.id, f"created by the test user ({t.id})", "created")
        self._record(g, "CREATE NEW", ok, r)
        if not u:
            for a in TYPE_ACTIONS[2:]:
                self._blocked(g, a, *self._missing(t))
            return
        # MODIFY title, MODIFY description
        ok, r = self.user.try_call("Tracker", "updateWorkItem", {"content": {"@uri": u, "t:title": f"{self.s.tag} edited by the test user"}}, write=True)
        self._record(g, "MODIFY", ok, r)
        ok, r = self.user.try_call("Tracker", "updateWorkItem", {"content": {"@uri": u, "t:description": self._text("edited by the test user")}}, write=True)
        self._record(g, "MODIFY FIELD (description)", ok, r)
        # ADD LINK (to the first other fixture)
        other = next((v for k, v in self.fixtures.items() if k != t.id), None)
        if other is None:                                                      # only one type under test: link to a second item of it
            _ok, _r, other = self._create_item(self.admin, t.id, f"link target ({t.id})", "link target")
        if other:
            ok, r = self.user.try_call("Tracker", "addLinkedItem", {"workitemURI": u, "linkedWorkitemURI": other, "role": {"t:id": "relates_to"}}, write=True)
            self._record(g, "ADD LINK", ok, r)
            if ok:
                self.admin.try_call("Tracker", "removeLinkedItem", {"workitemURI": u, "linkedWorkitemURI": other, "role": {"t:id": "relates_to"}}, write=True)
        else:
            self._blocked(g, "ADD LINK", "a second test Work Item is needed to link to")
        # DELETE (a disposable item made by the admin)
        ok, r, disp = self._create_item(self.admin, t.id, f"disposable ({t.id})", "disposable")
        if disp:
            ok, r = self.user.try_call("Tracker", "deleteWorkItem", {"workitemURI": disp}, write=True)
            self._record(g, "DELETE", ok, r)
        else:
            self._blocked(g, "DELETE", "a disposable Work Item could not be created")
        # COMMENT
        ok, r = self.user.try_call("Tracker", "createComment", {"workitemURI": u, "content": self._text("comment by the test user")}, write=True)
        self._record(g, "COMMENT", ok, r)
        # RESOLVE COMMENTS
        cu = self.comments.get(t.id)
        if cu:
            ok, r = self.user.try_call("Tracker", "setResolvedComment", {"commentURI": cu, "resolved": True}, write=True)
            self._record(g, "RESOLVE COMMENTS", ok, r)
            if ok:
                self.admin.try_call("Tracker", "setResolvedComment", {"commentURI": cu, "resolved": False}, write=True)
        else:
            self._blocked(g, "RESOLVE COMMENTS", "no test comment could be prepared")
        # APPROVE / DISAPPROVE (the user is an approvee of the fixture)
        appr = lambda status: {"content": {"@uri": u, "t:approvals": {"t:Approval": {"t:user": {"@uri": self.user_uri}, "t:status": {"t:id": status}}}}}  # noqa: E731
        ok, r = self.user.try_call("Tracker", "updateWorkItem", appr("approved"), write=True)
        self._record(g, "APPROVE/DISAPPROVE", ok, r)
        if ok:
            self.admin.try_call("Tracker", "updateWorkItem", appr("waiting"), write=True)
        # CHANGE STATUS through a workflow action the user is offered
        ok, r = self.user.try_call("Tracker", "getAvailableActions", {"workitemURI": u})
        aid = next((a.get("actionId") for a in (as_list(r) if ok else []) if isinstance(a, dict) and str(a.get("actionId", "")).isdigit()), None)
        if aid is None:
            self._blocked(g, "CHANGE STATUS (workflow)", "the workflow offers this user no action on the test Work Item", Cause.WORKFLOW)
        else:
            ok, r = self.user.try_call("Tracker", "performWorkflowAction", {"workitemURI": u, "actionId": int(aid)}, write=True)
            self._record(g, "CHANGE STATUS (workflow)", ok, r)
            if ok:
                self.admin.try_call("Tracker", "resetWorkflow", {"workitemURI": u}, write=True)

    @staticmethod
    def _unreadable(group: str, ok: bool, r) -> Probe:
        if not ok:
            outcome, cause, detail = classify(False, r)
            return Probe(group, "READ", outcome, cause, detail)
        return Probe(group, "READ", Outcome.DENIED, Cause.PERMISSION, "the test Work Item cannot be opened by this user")

    # ------------------------------------------------------------------ documents
    def _test_documents(self) -> None:
        g, type_id = "DOCUMENT", (self.s.types[0].id if self.s.types else "systemrequirement")
        name = f"PAVS-ROLE-{self.s.tag[11:-1]}"
        ok, r, doc = self._create_doc(self.admin, name + "-fixture", "fixture document", type_id)
        if not doc:
            _o, cause, detail = classify(ok, r)
            for a in DOCUMENT_ACTIONS:
                self._blocked(g, a, f"no test document could be prepared: {detail or str(r)[:160]}", cause if cause != Cause.PERMISSION else Cause.SETUP)
            return
        self.doc_uri = doc
        ok, r = self.user.try_call("Tracker", "getModuleByUri", {"uri": doc})
        body = r.get("getModuleByUriReturn", {}) if ok and isinstance(r, dict) else {}
        if ok and str(body.get("unresolvable")) == "false":
            self.probes.append(Probe(g, "READ", Outcome.ALLOWED))
        else:
            self.probes.append(self._unreadable(g, ok, r))
        ok, r, _ = self._create_doc(self.user, name + "-by-user", "created by the test user", type_id)
        self._record(g, "CREATE NEW", ok, r)
        ok, r = self.user.try_call("Tracker", "updateModule", {"module": {"@uri": doc, "t:title": f"{self.s.tag} edited by the test user"}}, write=True)
        self._record(g, "MODIFY FIELDS", ok, r)
        item = self._item(type_id, "created in a document by the test user")
        ok, r = self.user.try_call("Tracker", "createWorkItemInModule", {"moduleURI": doc, "parentWorkItemURI": None, "workItem": item}, write=True)
        if ok and uri_of(r):
            self.ledger.add("workitem", uri_of(r), item["t:title"])
        self._record(g, "MODIFY CONTENT", ok, r)
        ok, r, disp = self._create_doc(self.admin, name + "-disposable", "disposable document", type_id)
        if disp:
            ok, r = self.user.try_call("Tracker", "deleteModule", {"moduleURI": disp}, write=True)
            self._record(g, "DELETE", ok, r)
        else:
            self._blocked(g, "DELETE", "a disposable document could not be created")
        ok, r = self.user.try_call("Tracker", "createDocumentComment", {"documentURI": doc, "text": self._text("comment by the test user")}, write=True)
        self._record(g, "COMMENT", ok, r)

    # ------------------------------------------------------------------ permission questions the API cannot act out
    def _test_oracle(self) -> None:
        for group, action, perm in ORACLE:
            ok, r = self.admin.try_call("Security", "hasPermission", {"userId": self.s.user, "permission": "com.polarion." + perm, "projectId": self.s.project})
            if not ok:
                self.probes.append(Probe(group, action, Outcome.ERROR, Cause.SERVER, str(r)[:160], "SOAP Security.hasPermission (project-wide)"))
                continue
            granted = "true" in str(r).lower()
            self.probes.append(Probe(group, action, Outcome.ALLOWED if granted else Outcome.DENIED,
                                     Cause.NONE if granted else Cause.PERMISSION, "" if granted else "refused by the server's permission check",
                                     "SOAP Security.hasPermission (project-wide, indicative)"))

    # ------------------------------------------------------------------ cleanup
    def cleanup(self) -> tuple[int, list[str]]:
        """Delete every object this run created (documents first). Returns (deleted, leftovers). Never raises."""
        deleted, left = 0, []
        for kind, op, param in (("document", "deleteModule", "moduleURI"), ("workitem", "deleteWorkItem", "workitemURI")):
            for k, uri, title in self.ledger.open_items():
                if k != kind:
                    continue
                if not self._still_ours(kind, uri):
                    self.ledger.done(uri)                                  # gone already, or no longer ours: do not touch
                    continue
                ok, r = self.admin.try_call("Tracker", op, {param: uri}, write=True)
                if ok:
                    deleted += 1
                    self.ledger.done(uri)
                else:
                    left.append(f"{title}: {str(r)[:100]}")
        if left:
            self.notes.append(f"{len(left)} test object(s) could not be removed and are still recorded for the next clean-up: " + "; ".join(left[:3]))
        return deleted, left

    def _still_ours(self, kind: str, uri: str) -> bool:
        """True when the object still exists AND its title still carries this run's tag."""
        op, ret = ("getModuleByUri", "getModuleByUriReturn") if kind == "document" else ("getWorkItemByUri", "getWorkItemByUriReturn")
        ok, r = self.admin.try_call("Tracker", op, {"uri": uri})
        if not ok or not isinstance(r, dict):
            return False
        body = r.get(ret) or {}
        if str(body.get("unresolvable")) == "true":
            return False
        title = body.get("title")
        title = title.get("content") if isinstance(title, dict) else title
        return str(title or "").startswith(self.s.tag)
