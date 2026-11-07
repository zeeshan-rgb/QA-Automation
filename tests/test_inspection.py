"""Inspection engine end to end against the mock (REST/SOAP/SVN combinations, like real servers)."""
import pytest

from pavs.domain.config import SectionStatus
from pavs.domain.enums import AuthMethod, ProviderKind as P
from pavs.domain.models import Credentials
from pavs.engines.inspection import InspectionContext, InspectionEngine, check_permission
from pavs.polarion.connection import ConnectionManager
from pavs.services.inspection import diff_snapshots

from .conftest import ADMIN_PASSWORD, ADMIN_TOKEN, make_env

BOTH = Credentials(token="test-token-admin", password="admin-test-pw")


def _inspect(mock_server, profile, env, creds, **kw):
    app, wsgi = mock_server(profile)
    session = ConnectionManager(http_transport=wsgi).connect(env, creds)
    snap = InspectionEngine().inspect(session, "acme_demo", **kw)
    return app, session, snap


def _items(snap, area, name):
    return snap.section(area).items.get(name, [])


def test_2606_rest_soap_svn_complete(mock_server):
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=[P.REST, P.SOAP, P.SVN])
    app, s, snap = _inspect(mock_server, "2606", env, BOTH)
    statuses = {sec.area: sec.status for sec in snap.sections}
    assert all(v == SectionStatus.COMPLETE for v in statuses.values()), {
        sec.area: (sec.errors, [g.item for g in sec.gaps]) for sec in snap.sections if sec.status != "complete"}

    assert [t["id"] for t in _items(snap, "types", "types")] == ["requirement", "task", "testcase"]
    links = {l["id"]: l for l in _items(snap, "links", "link_roles")}
    assert links["verifies"]["opposite_name"] == "is verified by" and links["verifies"]["rules"] == [["testcase", "requirement"]]

    eff = [f for f in _items(snap, "fields", "effective") if f["target_type"] == "requirement"]
    assert {f["id"] for f in eff} == {"businessValue", "reviewer", "priorityClass", "safetyRelevant"}
    assert next(f for f in eff if f["id"] == "reviewer")["required"] is True
    assert snap.section("fields").providers[:1] == [P.REST]
    layers = _items(snap, "fields", "layers")
    assert {(f["scope"], f["target_type"]) for f in layers} == {("global", None), ("project", "requirement")}

    enums = {(e["enum_id"], e["target_type"]): e for e in _items(snap, "enumerations", "enumerations")}
    assert ("status", "requirement") in enums and enums[("status", "requirement")]["scope"] == "project"
    assert ("documents/document-type", None) in enums

    wf = {(w["prototype"], w["target_type"]): w for w in _items(snap, "workflow", "effective")}
    req = wf[("WorkItem", "requirement")]
    assert req["scope"] == "project" and req["initial_status"] == "draft"
    approve = next(a for a in req["actions"] if a["id"] == "approve")
    assert approve["conditions"][0]["name"] == "FieldNonEmpty"
    assert wf[("WorkItem", "task")]["scope"] == "global"           # falls back to the global generic workflow
    assert wf[("Module", "req_specification")]["actions"][0]["signature_policy"] == "all"
    acts = [a for a in _items(snap, "workflow", "item_actions") if a["work_item"] == "AD-1"]
    assert {a["action_id"] for a in acts if a["available"]} == {"review"}
    assert any(not a["available"] and a["unavailable_reason"] for a in acts)

    roles = {(r["scope"], r["id"]) for r in _items(snap, "roles", "roles")}
    assert ("project", "internal_reviewer") in roles and ("global", "admin") in roles
    asg = {(a["scope"], a["principal"], a["role"]) for a in _items(snap, "roles", "assignments")}
    assert ("project", "reviewer1", "internal_reviewer") in asg

    rules = _items(snap, "permissions", "rules")
    assert any(r["role"] == "internal_reviewer" and r["effect"] == "deny"
               and r["permission"] == "com.polarion.persistence.object.WorkItem.modify" for r in rules)
    assert len(_items(snap, "field_rules", "rules")) == 4
    assert [d["name"] for d in _items(snap, "documents", "documents")] == ["Release Notes", "System Requirements"]
    assert len(_items(snap, "pages", "pages")) == 2
    # inspection is read-only: no request other than GET / PROPFIND / SOAP POSTs of read operations
    assert {m for m, _ in app.requests} <= {"GET", "POST", "PROPFIND"}
    s.close()


def test_2410_rest_off_soap_only_has_gaps_not_failures(mock_server):
    env = make_env(auth_method=AuthMethod.PASSWORD)
    _, s, snap = _inspect(mock_server, "2410-rest-off", env, ADMIN_PASSWORD)
    sec = {x.area: x for x in snap.sections}
    assert [t["id"] for t in _items(snap, "types", "types")] == ["requirement", "task", "testcase"]
    assert sec["types"].providers == [P.SOAP]
    eff = [f for f in _items(snap, "fields", "effective") if f["target_type"] == "requirement"]
    assert {f["id"] for f in eff} >= {"reviewer", "priorityClass"}
    assert sec["fields"].status == SectionStatus.PARTIAL                       # SVN layers missing
    assert sec["permissions"].status == SectionStatus.UNAVAILABLE
    assert "SVN" in sec["permissions"].gaps[0].remedy
    assert sec["workflow"].status == SectionStatus.PARTIAL                     # definitions need SVN
    acts = _items(snap, "workflow", "item_actions")
    assert {a["action_id"] for a in acts if a["available"] and a["work_item"] == "AD-1"} == {"review"}
    asg = {(a["principal"], a["role"]) for a in _items(snap, "roles", "assignments")}
    assert ("reviewer1", "internal_reviewer") in asg                            # via SOAP getRolesForUser
    assert [d["name"] for d in _items(snap, "documents", "documents")] == ["Release Notes", "System Requirements"]
    assert sec["enumerations"].status == SectionStatus.PARTIAL                 # no listing on 2410 without SVN
    assert sec["field_rules"].status == SectionStatus.UNAVAILABLE
    s.close()


def test_2410_rest_on_uses_soap_for_fields_rest_for_rest(mock_server):
    _, s, snap = _inspect(mock_server, "2410", make_env(), ADMIN_TOKEN)
    assert snap.section("types").providers == [P.REST]
    assert snap.section("fields").providers == [P.SOAP]          # REST custom fields only from 2512
    docs = snap.section("documents")
    assert P.SOAP in docs.providers                              # REST document listing only from 2512
    assert len(docs.items["documents"]) == 2
    s.close()


def test_selected_areas_pull_in_dependencies(mock_server):
    _, s, snap = _inspect(mock_server, "2606", make_env(), ADMIN_TOKEN, areas=["fields"])
    assert [x.area for x in snap.sections] == ["types", "fields"]
    s.close()


def test_internal_reviewer_permission_check(mock_server):
    """The worked example from the brief: may review, may not modify or delete."""
    app, wsgi = mock_server("2606")
    s = ConnectionManager(http_transport=wsgi).connect(make_env(), ADMIN_TOKEN)
    ctx = InspectionContext(session=s, project_id="acme_demo")
    base = "com.polarion.persistence.object."
    assert check_permission(ctx, "reviewer1", base + "Comment.create").granted is True
    assert check_permission(ctx, "reviewer1", base + "WorkItem.read").granted is True
    assert check_permission(ctx, "reviewer1", base + "WorkItem.modify").granted is False
    assert check_permission(ctx, "reviewer1", base + "WorkItem.delete").granted is False
    s.close()


def test_permission_check_without_soap_is_not_a_guess(mock_server):
    env = make_env(providers_enabled=[P.REST])
    app, wsgi = mock_server("2606")
    s = ConnectionManager(http_transport=wsgi).connect(env, ADMIN_TOKEN)
    r = check_permission(InspectionContext(session=s, project_id="acme_demo"), "reviewer1", "x")
    assert r.granted is None and "SOAP" in r.detail
    s.close()


def test_diff_detects_changes(mock_server):
    from mock_polarion import repo_fixture
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=[P.REST, P.SOAP, P.SVN])
    _, s1, before = _inspect(mock_server, "2606", env, BOTH, areas=["types"])
    key = ".polarion/tracker/fields/workitem-type-enum.xml"
    original = repo_fixture.FILES[key]
    repo_fixture.FILES[key] = original.replace('<option id="task" name="Task" sortOrder="2"/>',
                                               '<option id="task" name="Work Task" sortOrder="2"/>'
                                               '<option id="risk" name="Risk" sortOrder="4"/>')
    try:
        _, s2, after = _inspect(mock_server, "2606", env, BOTH, areas=["types"])
    finally:
        repo_fixture.FILES[key] = original
    d = {x.area: x for x in diff_snapshots(before, after)}["types"]
    assert d.added == ["types:risk"] and d.changed == ["types:task"] and not d.removed
    s1.close()
    s2.close()


@pytest.mark.parametrize("profile", ["2506", "2512"])
def test_other_releases_complete_with_all_providers(mock_server, profile):
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=[P.REST, P.SOAP, P.SVN])
    _, s, snap = _inspect(mock_server, profile, env, BOTH)
    bad = {x.area: x.errors for x in snap.sections if x.status not in ("complete",)}
    assert not bad, bad
    s.close()
