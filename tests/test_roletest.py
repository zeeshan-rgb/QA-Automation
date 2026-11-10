"""Role test: outcome classification, Excel expectations, the probe runner (against a fake Polarion with real permission
rules), cleanup safety, the blockers report, and the role assignment / restore of user-roles.xml."""
import re

import pytest

from pavs.domain.enums import TestStatus as Status
from pavs.domain.errors import ConfigurationError, PavsError, WriteNotAuthorized
from pavs.engines.roletest.assign import Backup, RoleAssigner, roles_in, set_user_roles
from pavs.engines.roletest.expectations import Expect, load_matrix, sheet_names
from pavs.engines.roletest.model import Cause, Outcome, Probe, classify
from pavs.engines.roletest.report import build_report, evaluate
from pavs.engines.roletest.runner import MemoryLedger, RoleTestRunner, RunSetup, TypeSpec


# ---------------------------------------------------------------- classification
def test_classify_tells_permission_from_project_problems():
    assert classify(True)[0] == Outcome.ALLOWED
    o, c, d = classify(False, "com.polarion.PermissionDeniedException: Permission com.polarion.persistence.object.instance.key.title.modify denied for user user2")
    assert (o, c) == (Outcome.DENIED, Cause.PERMISSION) and "title.modify" in d
    assert classify(False, "Workflow initialization failed: the specified script was not found")[:2] == (Outcome.BLOCKED, Cause.WORKFLOW)
    assert classify(False, "user2 has no access to the Subversion Repository")[:2] == (Outcome.BLOCKED, Cause.REPO_ACCESS)
    assert classify(False, "The required field 'Category' (id: 'category') of Work Item 'X-1' is empty.")[:2] == (Outcome.BLOCKED, Cause.REQUIRED_FIELD)
    assert classify(False, "Not authorized.")[:2] == (Outcome.ERROR, Cause.SESSION)       # a dropped session is not a refusal
    assert classify(False, "boom")[:2] == (Outcome.ERROR, Cause.SERVER)


# ---------------------------------------------------------------- Excel expectations
@pytest.fixture
def sheet(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "P4"
    ws.append([None, None, None, "STAKEHOLDER NEED", None, None, None, "REQUEST", None, None])
    ws.append(["ROLE", "SOURCE", None, "READ", "CREATE NEW", "MODIFY", None, "READ", "MODIFY", "APPROVE/DISAPPROVE"])
    ws.append(["project_user", "OOTB", None, "True", None, None, None, "True", None, None])
    ws.append(["project_author", "CUSTOM", None, "True", "True", "False", None, "True", "True", "True"])
    ws.append([None])
    wb.create_sheet("Other")
    path = tmp_path / "roles.xlsx"
    wb.save(path)
    return path


def test_matrix_reads_blocks_and_values(sheet):
    assert sheet_names(sheet) == ["P4", "Other"]
    m = load_matrix(sheet, "P4")
    assert list(m.roles) == ["project_user", "project_author"]
    assert m.match_group("stakeholderneed", "Stakeholder Need") == "STAKEHOLDERNEED"
    assert m.match_group("changerequest", "Change Request") == "REQUEST"              # the sheet says REQUEST, Polarion says Change Request
    assert m.match_group("variable", "Variable") is None
    k = "STAKEHOLDERNEED"
    assert m.expect(["project_author"], k, "CREATE NEW") == Expect.YES
    assert m.expect(["project_author"], k, "MODIFY") == Expect.NO
    assert m.expect(["project_user"], k, "CREATE NEW") == Expect.BLANK
    assert m.expect(["project_user", "project_author"], k, "CREATE NEW") == Expect.YES      # two roles: what either grants
    assert m.expect(["project_author"], "REQUEST", "APPROVE/DISAPPROVE") == Expect.YES


def test_matrix_rejects_a_sheet_without_a_table(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["nothing here"])
    wb.save(tmp_path / "x.xlsx")
    with pytest.raises(ConfigurationError):
        load_matrix(tmp_path / "x.xlsx")


def test_evaluate_matches_the_sheet_semantics():
    allowed, denied = Probe("G", "A", Outcome.ALLOWED), Probe("G", "A", Outcome.DENIED, Cause.PERMISSION)
    blocked = Probe("G", "A", Outcome.BLOCKED, Cause.WORKFLOW)
    assert evaluate(allowed, Expect.YES)[:2] == (Status.PASS, "")
    assert evaluate(denied, Expect.YES)[:2] == (Status.FAIL, "blocker")
    assert evaluate(blocked, Expect.YES)[:2] == (Status.BLOCKED, "blocker")
    assert evaluate(denied, Expect.NO)[:2] == (Status.PASS, "")
    assert evaluate(allowed, Expect.NO)[:2] == (Status.FAIL, "unexpected")
    assert evaluate(allowed, Expect.BLANK)[:2] == (Status.PARTIAL_PASS, "unexpected")
    assert evaluate(blocked, Expect.NO)[:2] == (Status.NOT_TESTABLE, "untestable")
    assert evaluate(denied, Expect.INFO)[1] == ""                                    # no statement in the sheet: never a blocker


# ---------------------------------------------------------------- a fake Polarion with a real permission model
class FakeServer:
    """Work Items, comments and documents; every user may do only what `grants[user]` lists as (type-or-DOCUMENT, ACTION)."""

    def __init__(self, grants, broken_types=()):
        self.grants, self.broken_types = grants, set(broken_types)
        self.objs: dict[str, dict] = {}
        self.n = 0

    def uri(self, kind):
        self.n += 1
        return f"subterra:data-service:objects:/default/P${{{kind}}}X-{self.n}"

    def may(self, user, group, action):
        return user == "admin" or (group, action) in self.grants.get(user, set())


class FakeActor:
    def __init__(self, server, name):
        self.s, self.name = server, name

    def relogin(self):
        pass

    def deny(self, what):
        return False, f"com.polarion.PermissionDeniedException: Permission com.polarion.{what} denied for user {self.name}"

    def try_call(self, service, op, params=None, write=False):
        p, s = params or {}, self.s
        if service == "Security":
            who = p["userId"]
            return True, {"hasPermissionReturn": "true" if who == "admin" or "ORACLE" in s.grants.get(who, set()) else "false"}
        if op == "createWorkItem":
            t = p["content"]["t:type"]["t:id"]
            if t in s.broken_types:
                return False, "Workflow initialization failed: the specified script was not found"
            if not s.may(self.name, t, "CREATE NEW"):
                return self.deny("create")
            u = s.uri("WorkItem")
            s.objs[u] = {"kind": "workitem", "type": t, "title": p["content"]["t:title"], "comments": [], "approvee": set()}
            return True, {"createWorkItemReturn": u}
        if op in ("getWorkItemByUri", "getModuleByUri"):
            o = s.objs.get(p["uri"])
            if o is None:
                return True, {op + "Return": {"uri": p["uri"], "unresolvable": "true"}}
            if not s.may(self.name, o.get("type", "DOCUMENT"), "READ"):
                return True, {op + "Return": {"uri": p["uri"], "unresolvable": "true"}}
            body = {"uri": p["uri"], "unresolvable": "false", "title": o["title"]}
            if o["comments"]:
                body["comments"] = {"Comment": [{"uri": c} for c in o["comments"]]}
            return True, {op + "Return": body}
        if op == "updateWorkItem":
            o = s.objs[p["content"]["@uri"]]
            key = "t:title" if "t:title" in p["content"] else "t:description" if "t:description" in p["content"] else "t:approvals"
            action = {"t:title": "MODIFY", "t:description": "MODIFY FIELD", "t:approvals": "APPROVE/DISAPPROVE"}[key]
            if not s.may(self.name, o["type"], "MODIFY" if action != "APPROVE/DISAPPROVE" else action):
                return self.deny(action)
            if key == "t:title":
                o["title"] = p["content"]["t:title"]
            return True, {}
        if op == "deleteWorkItem":
            o = s.objs.get(p["workitemURI"])
            if o is None:
                return False, "not found"
            if not s.may(self.name, o["type"], "DELETE"):
                return self.deny("delete")
            del s.objs[p["workitemURI"]]
            return True, {}
        if op == "createComment":
            o = s.objs[p["workitemURI"]]
            if not s.may(self.name, o["type"], "COMMENT"):
                return self.deny("comment")
            c = s.uri("Comment")
            o["comments"].append(c)
            s.objs[c] = {"kind": "comment", "type": o["type"], "title": ""}
            return True, {}
        if op == "setResolvedComment":
            t = s.objs[p["commentURI"]]["type"]
            return (True, {}) if s.may(self.name, t, "RESOLVE COMMENTS") else self.deny("resolve")
        if op == "addApprovee":
            return True, {}
        if op in ("addLinkedItem", "removeLinkedItem"):
            t = s.objs[p["workitemURI"]]["type"]
            return (True, {}) if s.may(self.name, t, "MODIFY") else self.deny("link")
        if op == "getAvailableActions":
            return True, {"getAvailableActionsReturn": [{"actionId": "7"}]}
        if op == "performWorkflowAction":
            t = s.objs[p["workitemURI"]]["type"]
            return (True, {}) if s.may(self.name, t, "MODIFY") else self.deny("workflow")
        if op == "resetWorkflow":
            return True, {}
        if op == "createDocument":
            if not s.may(self.name, "DOCUMENT", "CREATE NEW"):
                return self.deny("create doc")
            u = s.uri("Module")
            s.objs[u] = {"kind": "document", "title": p["documentTitle"], "comments": []}
            return True, {"createDocumentReturn": u}
        if op == "updateModule":
            return (True, {}) if s.may(self.name, "DOCUMENT", "MODIFY FIELDS") else self.deny("doc fields")
        if op == "createWorkItemInModule":
            if not s.may(self.name, "DOCUMENT", "MODIFY CONTENT"):
                return self.deny("doc content")
            u = s.uri("WorkItem")
            s.objs[u] = {"kind": "workitem", "type": "systemrequirement", "title": p["workItem"]["t:title"], "comments": []}
            return True, {"createWorkItemInModuleReturn": u}
        if op == "deleteModule":
            if not s.may(self.name, "DOCUMENT", "DELETE"):
                return self.deny("doc delete")
            s.objs.pop(p["moduleURI"], None)
            return True, {}
        if op == "createDocumentComment":
            return (True, {}) if s.may(self.name, "DOCUMENT", "COMMENT") else self.deny("doc comment")
        raise AssertionError(f"unexpected call {service}.{op}")


TYPES = [TypeSpec("systemrequirement", "System Requirement"), TypeSpec("basictestcase", "Test Case")]
ALL_WI = {(t, a) for t in ("systemrequirement", "basictestcase") for a in
          ("READ", "CREATE NEW", "MODIFY", "DELETE", "COMMENT", "RESOLVE COMMENTS", "APPROVE/DISAPPROVE")}


def run(server, user="user2", types=TYPES, tag="[PAVS-ROLE-t1]", ledger=None):
    ledger = ledger or MemoryLedger()
    r = RoleTestRunner(FakeActor(server, "admin"), FakeActor(server, user), RunSetup("P", user, tag, types), ledger)
    return r, r.run(), ledger


def by(probes, group, action):
    return next(p for p in probes if p.group == group and p.action == action)


def test_runner_exercises_every_action_and_removes_everything():
    server = FakeServer({"user2": ALL_WI | {("DOCUMENT", a) for a in ("READ", "CREATE NEW", "MODIFY FIELDS", "MODIFY CONTENT", "DELETE", "COMMENT")} | {"ORACLE"}})
    r, probes, ledger = run(server)
    assert all(p.outcome == Outcome.ALLOWED for p in probes), [(p.group, p.action, p.outcome) for p in probes if p.outcome != Outcome.ALLOWED]
    assert {p.action for p in probes if p.group == "SYSTEM REQUIREMENT"} >= {"READ", "CREATE NEW", "MODIFY", "MODIFY FIELD (description)", "ADD LINK", "DELETE",
                                                                              "COMMENT", "RESOLVE COMMENTS", "APPROVE/DISAPPROVE", "CHANGE STATUS (workflow)"}
    assert {p.action for p in probes if p.group == "DOCUMENT"} >= {"CREATE NEW", "MODIFY FIELDS", "MODIFY CONTENT", "DELETE", "COMMENT", "MANAGE"}
    leftovers = [o for o in server.objs.values() if o["kind"] != "comment"]
    assert leftovers == [] and ledger.open_items() == []          # nothing the test created is left behind


def test_runner_reports_what_the_role_cannot_do():
    server = FakeServer({"user2": {("systemrequirement", a) for a in ("READ", "COMMENT")} | {("basictestcase", "READ")}})
    _r, probes, ledger = run(server)
    assert by(probes, "SYSTEM REQUIREMENT", "READ").outcome == Outcome.ALLOWED
    assert by(probes, "SYSTEM REQUIREMENT", "COMMENT").outcome == Outcome.ALLOWED
    for action in ("CREATE NEW", "MODIFY", "MODIFY FIELD (description)", "ADD LINK", "DELETE", "RESOLVE COMMENTS", "APPROVE/DISAPPROVE"):
        p = by(probes, "SYSTEM REQUIREMENT", action)
        assert (p.outcome, p.cause) == (Outcome.DENIED, Cause.PERMISSION), action
    assert by(probes, "DOCUMENT", "CREATE NEW").outcome == Outcome.DENIED
    assert by(probes, "TEST RUN", "READ").outcome == Outcome.DENIED
    assert [o for o in server.objs.values() if o["kind"] != "comment"] == [] and ledger.open_items() == []


def test_a_broken_project_is_a_blocker_with_its_cause_not_a_permission_problem():
    server = FakeServer({"user2": ALL_WI}, broken_types={"basictestcase"})
    _r, probes, _ = run(server)
    for action in ("READ", "CREATE NEW", "MODIFY", "DELETE", "COMMENT", "APPROVE/DISAPPROVE"):
        p = by(probes, "TEST CASE", action)
        assert (p.outcome, p.cause) == (Outcome.BLOCKED, Cause.WORKFLOW), (action, p)
    assert "script" in by(probes, "TEST CASE", "READ").detail
    assert by(probes, "SYSTEM REQUIREMENT", "CREATE NEW").outcome == Outcome.ALLOWED          # the other type is unaffected


def test_cleanup_never_deletes_an_object_that_lost_the_tag():
    server = FakeServer({})
    u = server.uri("WorkItem")
    server.objs[u] = {"kind": "workitem", "type": "systemrequirement", "title": "A REAL requirement somebody renamed", "comments": []}
    ledger = MemoryLedger()
    ledger.add("workitem", u, "[PAVS-ROLE-t1] fixture")
    runner = RoleTestRunner(FakeActor(server, "admin"), FakeActor(server, "user2"), RunSetup("P", "user2", "[PAVS-ROLE-t1]", []), ledger)
    assert runner.cleanup() == (0, [])
    assert u in server.objs and ledger.open_items() == []        # left alone, and no longer on the list to retry


# ---------------------------------------------------------------- the report
def test_report_without_a_sheet_expects_everything_to_work_and_lists_blockers():
    server = FakeServer({"user2": {("systemrequirement", a) for a in ("READ", "CREATE NEW")}}, broken_types={"basictestcase"})
    _r, probes, _ = run(server)
    rep = build_report("user2", ["project_x"], "P", probes, TYPES, None)
    assert not rep.passed
    kinds = {(f.group, f.action): f for f in rep.blockers}
    assert kinds[("SYSTEM REQUIREMENT", "MODIFY")].cause == Cause.PERMISSION
    assert kinds[("TEST CASE", "CREATE NEW")].cause == Cause.WORKFLOW
    roots = rep.root_causes()
    assert roots and roots[0][0][0] == Cause.WORKFLOW and roots[0][1] >= 5
    text = rep.text()
    assert "NOT PASSED" in text and "BLOCKERS" in text and "script" in text
    assert any(r.status in (Status.FAIL, Status.BLOCKED) for r in rep.results)


def test_report_passes_when_the_role_can_do_all_that_the_sheet_grants(sheet):
    m = load_matrix(sheet, "P4")
    server = FakeServer({"user2": {("systemrequirement", "READ")}})
    _r, probes, _ = run(server, types=[TypeSpec("stakeholderneed", "Stakeholder Need")])
    # project_user: READ only in the sheet; everything else is refused by the fake, as the sheet expects
    server = FakeServer({"user2": {("stakeholderneed", "READ"), ("DOCUMENT", "READ"), ("TEST RUN", "READ"), "ORACLE"}})
    _r, probes, _ = run(server, types=[TypeSpec("stakeholderneed", "Stakeholder Need")])
    rep = build_report("user2", ["project_user"], "P", probes, [TypeSpec("stakeholderneed", "Stakeholder Need")], m)
    assert rep.passed and not [f for f in rep.findings if f.kind == "blocker"]
    assert rep.expectation.startswith("sheet 'P4'")


def test_report_flags_access_the_sheet_does_not_grant(sheet):
    m = load_matrix(sheet, "P4")
    types = [TypeSpec("stakeholderneed", "Stakeholder Need")]
    server = FakeServer({"user2": {("stakeholderneed", a) for a in ("READ", "CREATE NEW", "MODIFY")}})
    _r, probes, _ = run(server, types=types)
    rep = build_report("user2", ["project_author"], "P", probes, types, m)       # the sheet: author may create, MODIFY = False
    unexpected = {(f.group, f.action) for f in rep.findings if f.kind == "unexpected"}
    assert ("STAKEHOLDER NEED", "MODIFY") in unexpected and ("STAKEHOLDER NEED", "ADD LINK") in unexpected     # a link edit is a modify
    assert ("STAKEHOLDER NEED", "CREATE NEW") not in unexpected


# ---------------------------------------------------------------- roles in user-roles.xml
ORIGINAL = '<?xml version="1.0" encoding="UTF-8"?>\r\n<user-roles>\r\n    <user name="admin">\r\n        <role name="project_admin"/>\r\n    </user>\r\n' \
           '    <user name="user2">\r\n        <role name="project_user"/>\r\n    </user>\r\n</user-roles>\r\n'


def test_set_user_roles_changes_only_that_user():
    new = set_user_roles(ORIGINAL, "user2", ["project_reviewer", "project_approver"])
    assert roles_in(new, "user2") == ["project_reviewer", "project_approver"]
    assert roles_in(new, "admin") == ["project_admin"]
    assert roles_in(set_user_roles(ORIGINAL, "user2", []), "user2") == []
    fresh = set_user_roles(None, "u", ["r"])
    assert roles_in(fresh, "u") == ["r"] and fresh.startswith("<?xml")
    with pytest.raises(PavsError):
        set_user_roles("<user-roles><user name='x'>", "u", ["r"])          # a broken file is never overwritten


class FakeSvn:
    """Plays the Subversion client: `checkout` puts the repository's file in the folder, `commit` stores it back."""

    def __init__(self, text):
        self.text, self.commits, self.calls = text, [], []

    def __call__(self, cmd, cwd=None, input=None, **kw):
        import subprocess
        from pathlib import Path
        sub = cmd[1]
        self.calls.append((sub, input))
        if sub == "checkout":
            if self.text is not None:
                (Path(cmd[5]) / "user-roles.xml").write_bytes(self.text.encode())
        elif sub == "commit":
            f = Path(cwd) / "user-roles.xml"
            self.commits.append(f.read_bytes().decode() if f.exists() else None)
            self.text = self.commits[-1]
        elif sub == "delete":
            (Path(cwd) / "user-roles.xml").unlink()
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_assign_and_restore_put_back_the_exact_original_bytes():
    svn = FakeSvn(ORIGINAL)
    a = RoleAssigner("svn", "file:///repo/P/.polarion/security", "pavs", "secret", run=svn)
    backup = a.assign("user2", ["project_reviewer"])
    assert backup.original == ORIGINAL and roles_in(svn.text, "user2") == ["project_reviewer"]
    assert all(c[1] == "secret\n" for c in svn.calls if c[1])            # the password goes in on stdin, never on the command line
    assert a.restore(backup) is True
    assert svn.text == ORIGINAL                                          # CRLF and all
    assert a.restore(backup) is False                                    # already back: nothing to commit


def test_assign_when_the_file_did_not_exist_removes_it_again():
    svn = FakeSvn(None)
    a = RoleAssigner("svn", "file:///repo/P/.polarion/security", "pavs", run=svn)
    backup = a.assign("u", ["r"])
    assert backup.original is None and roles_in(svn.text, "u") == ["r"]
    a.restore(backup)
    assert svn.text is None


def test_assign_with_nothing_to_change_commits_nothing():
    svn = FakeSvn(set_user_roles(ORIGINAL, "user2", ["project_reviewer"]))
    a = RoleAssigner("svn", "file:///x", "pavs", run=svn)
    a.assign("user2", ["project_reviewer"])
    assert svn.commits == []


def test_a_failing_svn_error_never_shows_the_password():
    import subprocess

    def boom(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "svn: E170001: Authentication failed for pw-123456")
    with pytest.raises(PavsError) as ei:
        RoleAssigner("svn", "http://x/repo", "u", "pw-123456", run=boom).assign("u", ["r"])
    assert "pw-123456" not in str(ei.value)


def test_backup_round_trips_through_json():
    b = Backup("user2", "file:///x", ORIGINAL, "pavs")
    assert Backup.from_dict(b.to_dict()) == b


# ---------------------------------------------------------------- the service's safety gates
@pytest.fixture(scope="module")
def qapp_rt():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def svc_ctx(tmp_path, mock_server):
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend
    from .conftest import ADMIN_TOKEN, make_env
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "rt.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    env = ctx.environments.save(make_env(name="Mock 2606"), ADMIN_TOKEN)
    ctx.connections.connect(env)
    yield ctx, env
    ctx.close()


def test_run_needs_the_lead_role_an_approved_project_and_a_password(svc_ctx):
    from pavs.services.roles import AppRole, RoleDenied
    ctx, env = svc_ctx
    ctx.roles.set_role(AppRole.TESTER)
    with pytest.raises(RoleDenied):
        ctx.roletest.run(env.id, "acme_demo", "u", "pw", ["r"])
    ctx.roles.set_role(AppRole.LEAD)
    with pytest.raises(ConfigurationError):
        ctx.roletest.run(env.id, "acme_demo", "u", "", ["r"])                    # no password
    with pytest.raises(ConfigurationError):
        ctx.roletest.run(env.id, "acme_demo", "u", "pw", [])                     # automatic assignment needs a role
    with pytest.raises(WriteNotAuthorized):
        ctx.roletest.run(env.id, "acme_demo", "u", "pw", ["r"])                  # the project was never approved for test data


def test_pending_restore_survives_in_the_database(svc_ctx):
    import json
    ctx, env = svc_ctx
    ctx.roletest.repo.set_pending("run1", env.id, "acme_demo", json.dumps(Backup("user2", "file:///x", ORIGINAL, "pavs").to_dict()))
    assert ctx.roletest.pending_restores(env.id) == [("run1", "acme_demo", "user2")]


# ---------------------------------------------------------------- the page
def test_role_test_page_shows_users_and_the_blockers_report(svc_ctx, qapp_rt):
    from pavs.engines.roletest.runner import TypeSpec as T
    from pavs.services.roletest import AssignCapability, RoleTestOutcome, UserInfo
    from pavs.ui.pages.role_test import RoleTestPage
    ctx, env = svc_ctx
    page = RoleTestPage(ctx)
    page.refresh()
    assert page.env_box.count() == 1 and page.project_box.count() >= 1
    page._loaded(([UserInfo("user1", "User One"), UserInfo("user2", "user2")], ["project_reviewer", "project_user"],
                  [T("systemrequirement", "System Requirement")], ["Requirements", "_default"], AssignCapability(True, "", "svn", "file:///x")))
    assert page.users.count() == 2 and page.roles.count() == 2 and page.types.count() == 1
    assert page.space_box.currentText() == "Requirements" and page.mode_auto.isEnabled()
    server = FakeServer({"user2": {("systemrequirement", "READ")}}, broken_types=set())
    types = [T("systemrequirement", "System Requirement")]
    _r, probes, _ = run(server, types=types)
    rep = build_report("user2", ["project_reviewer"], "acme_demo", probes, types, None)
    from pavs.domain.testing import TestRunRecord
    rec = TestRunRecord(environment_id=env.id, project_id="acme_demo", kind="role_test")
    rec.results = rep.results
    page._done(RoleTestOutcome(rep, rec, True))
    assert "NOT PASSED" in page.summary.text()
    assert "BLOCKERS" in page.report_text.toPlainText()
    assert page.table.rowCount() == len(rep.results) and page.table.item(0, 0).text() in ("FAIL", "ERROR", "BLOCKED")
    assert page.btn_export.isEnabled()
    # without svn the page says why and falls back to 'already assigned'
    page._loaded(([UserInfo("user2", "user2")], ["r"], types, ["_default"], AssignCapability(False, "svn.exe was not found")))
    assert page.mode_have.isChecked() and not page.mode_auto.isEnabled() and "svn.exe" in page.cap_note.text()
