"""Health checks and the requirement executor, on mock data and on deliberately broken configuration."""
import pytest

from mock_polarion import repo_fixture
from pavs.domain.enums import AuthMethod, ProviderKind as P, TestStatus as S
from pavs.engines.inspection import InspectionEngine
from pavs.engines.requirements.importers import import_text
from pavs.engines.requirements.rules import Names, generate_for_requirement
from pavs.engines.testing.executor import Executor
from pavs.engines.testing.health import run_health_checks
from pavs.engines.testing.view import ConfigView
from pavs.polarion.connection import ConnectionManager

from .conftest import ADMIN_PASSWORD, make_env

ALL = [P.REST, P.SOAP, P.SVN]


def snapshot(mock_server, profile="2606", providers=ALL, **kw):
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=providers)
    from pavs.domain.models import Credentials
    app, wsgi = mock_server(profile)
    s = ConnectionManager(http_transport=wsgi).connect(env, Credentials(token="test-token-admin", password="admin-test-pw"))
    return s, InspectionEngine().inspect(s, "acme_demo", **kw)


def by_id(results):
    return {r.check_id: r for r in results}


def test_health_checks_pass_on_a_consistent_configuration(mock_server):
    s, snap = snapshot(mock_server)
    res = by_id(run_health_checks(ConfigView(snap)))
    assert len(res) == 14
    bad = {k: (v.status.value, v.evidence) for k, v in res.items() if v.status not in (S.PASS, S.PARTIAL_PASS)}
    assert not bad, bad
    s.close()


@pytest.mark.parametrize("file,old,new,check,fragment", [
    (".polarion/tracker/workflow/workflow.xml", 'to="done"/>\n        <transition action="reopen"',
     'to="ghost"/>\n        <transition action="reopen"', "H02", "ghost"),
    (".polarion/tracker/workflow/workflow.xml", '<action id="reopen" name="Reopen"/>', "", "H03", "reopen"),
    (".polarion/tracker/workflow/workflow.xml", 'initial-status="open" ', "", "H04", "initial status"),
    (".polarion/tracker/fields/custom-fields.xml", 'type="integer"', 'type="enum:doesnotexist"', "H07", "doesnotexist"),
    (".polarion/tracker/fields/priority-enum.xml", '<option id="low" name="Low" default="true"/>',
     '<option id="low" name="Low" default="true"/><option id="high" name="Dup" default="true"/>', "H08", "more than once"),
    (".polarion/security/permissions.xml", '<deny permission="@Module_Released.modifyContent"/>',
     '<deny permission="@Nope.modifyContent"/>', "H12", "@Nope"),
])
def test_health_checks_detect_real_problems(mock_server, file, old, new, check, fragment):
    original = repo_fixture.FILES[file]
    assert old in original, "fixture text changed"
    repo_fixture.FILES[file] = original.replace(old, new)
    try:
        s, snap = snapshot(mock_server)
    finally:
        repo_fixture.FILES[file] = original
    r = by_id(run_health_checks(ConfigView(snap)))[check]
    assert r.status == S.FAIL, r
    assert any(fragment in e for e in r.evidence), r.evidence
    s.close()


def test_health_checks_are_blocked_not_failed_without_config_files(mock_server):
    """SOAP only: workflow definitions cannot be read, so those checks say BLOCKED with the fix, never FAIL."""
    env = make_env(auth_method=AuthMethod.PASSWORD)
    app, wsgi = mock_server("2410-rest-off")
    s = ConnectionManager(http_transport=wsgi).connect(env, ADMIN_PASSWORD)
    snap = InspectionEngine().inspect(s, "acme_demo")
    res = by_id(run_health_checks(ConfigView(snap)))
    assert res["H05"].status == S.BLOCKED and "SVN" in " ".join(res["H05"].evidence)
    assert not [r for r in res.values() if r.status == S.FAIL]
    s.close()


def run_req(mock_server, text, snap_kwargs=None, server=True, providers=ALL):
    s, snap = snapshot(mock_server, providers=providers)
    view = ConfigView(snap)
    from pavs.engines.inspection import InspectionContext, check_permission
    ictx = InspectionContext(session=s, project_id="acme_demo")
    ex = Executor(view, (lambda u, p: check_permission(ictx, u, p).granted) if server else None, soap_available=server)
    out = []
    for r in import_text(text).requirements:
        for tc in generate_for_requirement(r, Names.from_view(view)).cases:
            out.append(ex.run(tc, r.label))
    s.close()
    return out


def test_internal_reviewer_requirement_passes_with_server_evidence(mock_server):
    res = run_req(mock_server, "REQ-1: Create a role called Internal Reviewer. The role should have permission to review "
                               "Work Items but should not have permission to modify or delete them.")
    assert len(res) == 5 and all(r.status == S.PASS for r in res), [(r.name, r.status, r.actual) for r in res]
    perm = [r for r in res if "modify" in r.name][0]
    assert perm.mechanism == "SOAP Security.hasPermission" and any(u in e for e in perm.evidence for u in ("reviewer1", "tester1", "roapi1"))


def test_permission_config_only_is_partial_not_pass(mock_server):
    res = run_req(mock_server, "The role internal_reviewer cannot modify Work Items.", server=False)
    assert res[0].status == S.PARTIAL_PASS and "Not confirmed by the server" in " ".join(res[0].evidence)


def test_wrong_expectation_fails_with_evidence(mock_server):
    res = run_req(mock_server, "The role internal_reviewer can modify Work Items.")
    assert res[0].status == S.FAIL and "denied" in res[0].actual


@pytest.mark.parametrize("text,status,fragment", [
    ("The field Reviewer is mandatory for Requirement work items.", S.PASS, "required"),
    ("The field Business Value is mandatory for Requirement work items.", S.FAIL, "not required"),
    ("The field Colour is mandatory for Requirement work items.", S.FAIL, "does not exist"),
    ("Requirement work items can move from Draft to Reviewed.", S.PASS, "allowed via review"),
    ("Requirement work items cannot move from Draft to Approved.", S.PASS, "no such transition"),
    ("Requirement work items can move from Draft to Approved.", S.FAIL, "no such transition"),
    ("The review action requires the field Reviewer for Requirement work items.", S.PASS, "required"),
    ("The Priority enumeration must contain the values High and Low.", S.PASS, "all present"),
    ("The Priority enumeration must contain the values High and Urgent.", S.FAIL, "missing: Urgent"),
    ("Define a work item type called Risk.", S.FAIL, "does not exist"),
    ("Create a role called Auditor.", S.FAIL, "does not exist"),
    ("Add a document type called Generic.", S.PASS, "exists"),
    ("Every Testcase must be linked to a Requirement.", S.PARTIAL_PASS, "configuration allows"),
])
def test_requirement_verdicts(mock_server, text, status, fragment):
    res = run_req(mock_server, text)
    assert len(res) >= 1
    assert res[0].status == status, (res[0].name, res[0].actual, res[0].evidence)
    assert fragment in res[0].actual


def test_missing_data_is_blocked_with_the_fix(mock_server):
    env = make_env(auth_method=AuthMethod.PASSWORD)
    app, wsgi = mock_server("2410-rest-off")
    s = ConnectionManager(http_transport=wsgi).connect(env, ADMIN_PASSWORD)
    view = ConfigView(InspectionEngine().inspect(s, "acme_demo"))
    ex = Executor(view)
    tc = generate_for_requirement(import_text("Requirement work items can move from Draft to Reviewed.").requirements[0]).cases[0]
    r = ex.run(tc)
    assert r.status == S.BLOCKED and "SVN" in " ".join(r.evidence)
    s.close()


def test_custom_check_is_not_testable_and_crash_is_contained():
    from pavs.domain.requirements import CheckKind, CheckSpec, Expectation, TestCase
    from pavs.domain.config import ConfigSnapshot
    ex = Executor(ConfigView(ConfigSnapshot(environment_id="e", project_id="p", release=None, adapter="")))
    tc = TestCase(requirement_id="r", title="manual", check=CheckSpec(kind=CheckKind.CUSTOM, expect=Expectation.PRESENT))
    assert ex.run(tc).status == S.NOT_TESTABLE
    bad = TestCase(requirement_id="r", title="x", check=CheckSpec(kind=CheckKind.ROLE_EXISTS, expect=Expectation.PRESENT, target={}))
    assert ex.run(bad).status in (S.ERROR, S.BLOCKED)


def test_merged_soap_enum_listing_is_not_reported_as_duplicates():
    """Tracker.getAllEnumOptionsForId returns the options of every Work Item type's variant of an enumeration, so ids repeat
    and several defaults appear. That is the server's data, not a configuration defect (seen on a real 2606 project)."""
    from pavs.domain.config import EnumOptionDef
    from pavs.engines.inspection.enums import unique_options
    from pavs.engines.testing.health import h_enum_options

    opts = unique_options([EnumOptionDef(id=i, default=d) for i, d in
                           [("done", True), ("valid", True), ("done", False), ("invalid", False), ("invalid", False)]])
    assert [o.id for o in opts] == ["done", "valid", "invalid"]

    class View:
        def __init__(self, enums):
            self._e = enums

        def enumerations(self):
            return self._e

    merged = {"enum_id": "resolution", "target_type": None, "options": [{"id": "done", "default": True}, {"id": "valid", "default": True}],
              "sources": [{"location": "Tracker.getAllEnumOptionsForId(resolution)"}]}
    errs, _, _ = h_enum_options(View([merged]))
    assert errs == []
    from_file = dict(merged, sources=[{"location": "/.polarion/tracker/fields/resolution-enum.xml"}])
    errs, _, _ = h_enum_options(View([from_file]))
    assert any("more than one default" in e for e in errs)                      # a real file with two defaults is still flagged
    dup = dict(from_file, options=[{"id": "a"}, {"id": "a"}])
    assert any("more than once" in e for e in h_enum_options(View([dup]))[0])
