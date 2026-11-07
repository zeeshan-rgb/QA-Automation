"""Phase 4: real Work Item data checks, guarded behavioural tests, and proof that writes stay safe."""
import pytest

from mock_polarion import PROFILES, MockPolarion
from mock_polarion.workitems import SEED
from pavs.app import build_context
from pavs.domain.enums import EnvironmentKind, TestStatus as S
from pavs.domain.errors import ConfigurationError, WriteNotAuthorized
from pavs.engines.testing.data import coverage, load_items
from pavs.infra.vault import MemoryBackend

URL = "https://mock.test/polarion"
PROJECT = "acme_demo"
SEED_IDS = {p: sorted(i["id"] for i in items) for p, items in SEED.items()}

REVIEWER = ("The role internal_reviewer cannot modify Work Items. The role cannot delete Work Items. "
            "The role cannot create Work Items. The role can comment on Work Items. The role can read Work Items.")


def store_ids(app):
    return {p: sorted(d) for p, d in app.store.items.items()}


@pytest.fixture
def world(tmp_path):
    """connect (token + password) -> context; mock app exposed so tests can inspect what really happened."""
    import httpx

    def make(profile="2606", kind=EnvironmentKind.DEVELOPMENT, approve=True, accounts=True):
        app = MockPolarion(PROFILES[profile])
        ctx = build_context(db_path=tmp_path / f"{profile}.sqlite3", vault_backend=MemoryBackend(),
                            http_transport=httpx.WSGITransport(app=app), log_to_file=False)
        out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw", token="test-token-admin")
        assert out.ok, out.message
        env = out.environment
        if kind != EnvironmentKind.DEVELOPMENT:
            env = ctx.environments.save(env.model_copy(update={"kind": kind}))
            ctx.connections.connect(env)
        if approve:
            ctx.environments.allow_writes_in(env.id, PROJECT, "tester")
        if accounts:
            ctx.environments.set_test_account(env.id, "internal_reviewer", "tester1", "test-token-tester")
            ctx.environments.set_test_account(env.id, "project_assignable", "dev1", "test-token-dev")
        return ctx, app, ctx.environments.get(env.id)
    return make


# ---------------------------------------------------------------- read-only data checks
def test_items_via_soap_and_rest_agree(world):
    ctx, app, env = world()
    session = ctx.connections.sync_environment(env.id)
    soap = load_items(type("S", (), {"soap": session.soap, "rest": None})(), PROJECT)
    rest = load_items(type("S", (), {"soap": None, "rest": session.rest})(), PROJECT)
    assert soap.source == "SOAP" and rest.source == "REST"
    assert {k: (v.type, v.status, sorted(v.links)) for k, v in soap.items.items()} == \
           {k: (v.type, v.status, sorted(v.links)) for k, v in rest.items.items()}
    covered, uncovered = coverage(soap, "requirement", "testcase")
    assert [i.id for i in covered] == ["AD-1"] and [i.id for i in uncovered] == ["AD-4"]
    ctx.close()


def test_traceability_uses_real_data(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("T-1: Every Requirement must be linked to a Testcase.", "t", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id)
    r = run.results[0]
    assert r.status == S.FAIL and "1 of 2" in r.actual and "50.0% coverage" in r.actual
    assert "Not linked: AD-4" in r.evidence
    app.store.items[PROJECT]["AD-4"]["links"].append(("verifies", "AD-3"))      # link AD-4 to the test case
    app.store.items[PROJECT]["AD-3"]["links"].append(("verifies", "AD-4"))
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id)
    assert run.results[0].status == S.PASS and "2 of 2" in run.results[0].actual
    assert not app.store.writes                                                    # read-only
    ctx.close()


def test_data_checks_find_real_problems(world):
    ctx, app, env = world()
    clean = {r.check_id: r for r in ctx.testing.run(env.id, PROJECT, health=False, data=True).results}
    assert clean["D01"].status == S.PASS and clean["D02"].status == S.PASS
    assert clean["D03"].status == S.PARTIAL_PASS and "AD-4" in " ".join(clean["D03"].evidence)

    app.store.items[PROJECT]["AD-5"]["status"] = "weird"
    app.store.items[PROJECT]["AD-2"]["links"].append(("relates_to", "AD-999"))
    bad = {r.check_id: r for r in ctx.testing.run(env.id, PROJECT, health=False, data=True).results}
    assert bad["D01"].status == S.FAIL and "AD-5" in " ".join(bad["D01"].evidence)
    assert bad["D02"].status == S.FAIL and "AD-999" in " ".join(bad["D02"].evidence)
    assert not app.store.writes
    ctx.close()


# ---------------------------------------------------------------- behavioural: workflow
def test_workflow_transition_is_really_executed_and_cleaned_up(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("W-1: Task work items can move from Open to Done.", "w", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status == S.PASS and r.mechanism == "REST (behavioural test)"
    assert any("now in status 'done'" in e for e in r.evidence)
    clean = next(x for x in run.results if x.check_id == "B99")
    assert clean.status == S.PASS and "1 created, all removed" in clean.actual
    assert store_ids(app) == SEED_IDS                                              # nothing left behind
    assert not ctx.testing.leftover_artifacts(env.id)
    ctx.close()


def test_required_user_field_is_filled_automatically(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("W-2: Requirement work items can move from Draft to Reviewed.", "w", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status == S.PASS, (r.actual, r.evidence)          # the 'review' action requires the Reviewer user field
    ctx.close()


def test_server_refusal_of_a_configured_transition_is_a_failure(world):
    ctx, app, env = world()
    app.store.refuse_actions.add("close")
    a = ctx.testing.import_pasted("W-3: Task work items can move from Open to Done.", "w", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status == S.FAIL and "rejected action 'close'" in " ".join([r.actual] + r.evidence)
    assert store_ids(app) == SEED_IDS
    ctx.close()


# ---------------------------------------------------------------- behavioural: permissions as a real test account
def test_permissions_really_tried_as_the_test_account(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("P-1: " + REVIEWER, "p", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    res = [x for x in run.results if x.group == "Requirements"]
    assert len(res) == 5 and all(x.status == S.PASS for x in res), [(x.name, x.status.value, x.actual) for x in res]
    assert all("really" in x.actual for x in res)
    mod = next(x for x in res if "modify" in x.name)
    assert any("refused" in e for e in mod.evidence) and any("tester1" in e for e in mod.evidence)
    # the account really created/changed nothing it was not allowed to
    assert not [w for w in app.store.writes if w[0] == "tester1" and w[1] in ("PATCH", "DELETE")]
    assert store_ids(app) == SEED_IDS
    ctx.close()


def test_wrong_requirement_is_failed_by_real_behaviour(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("P-2: The role internal_reviewer can modify Work Items.", "p", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status == S.FAIL
    assert any("Behavioural test: really refused" in e for e in r.evidence)
    ctx.close()


def test_granted_permission_assignable_role(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("P-3: The role project_assignable can modify Work Items. The role cannot delete Work Items.",
                                  "p", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    res = [x for x in run.results if x.group == "Requirements"]
    assert [x.status for x in res] == [S.PASS, S.PASS], [(x.name, x.actual) for x in res]
    assert ("dev1", "PATCH") in {(u, m) for u, m, p, r in app.store.writes}            # it really modified a test item
    ctx.close()


def test_account_without_rest_endpoint_permission_is_blocked_not_guessed(world):
    ctx, app, env = world()
    ctx.environments.set_test_account(env.id, "internal_reviewer", "roapi1", "test-token-roapi")
    a = ctx.testing.import_pasted("P-4: The role internal_reviewer cannot modify Work Items.", "p", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status == S.PARTIAL_PASS                                      # server answer stands; behaviour inconclusive
    assert any("lacks the REST endpoint permission" in e for e in r.evidence)
    ctx.close()


def test_bad_token_for_test_account_is_blocked(world):
    ctx, app, env = world()
    ctx.environments.set_test_account(env.id, "internal_reviewer", "tester1", "wrong-token-xx")
    a = ctx.testing.import_pasted("P-5: The role internal_reviewer cannot modify Work Items.", "p", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    r = next(x for x in run.results if x.group == "Requirements")
    assert r.status in (S.PARTIAL_PASS, S.PASS) and any("token was rejected" in e for e in r.evidence)
    ctx.close()


# ---------------------------------------------------------------- SAFETY
def test_no_write_without_explicit_project_approval(world):
    ctx, app, env = world(approve=False)
    a = ctx.testing.import_pasted("S-1: Task work items can move from Open to Done.", "s", env.id, PROJECT)
    with pytest.raises(WriteNotAuthorized, match="has not been approved"):
        ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    assert app.store.writes == [] and store_ids(app) == SEED_IDS
    ctx.close()


def test_writes_only_ever_reach_the_approved_project(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("S-2: Task work items can move from Open to Done. " + REVIEWER, "s", env.id, PROJECT)
    ctx.testing.run(env.id, PROJECT, health=True, data=True, set_id=a.requirement_set.id, behavioural=True)
    assert app.store.writes and {p for _, _, p, _ in app.store.writes} == {PROJECT}
    # a run against another project cannot write at all, even though the same environment is approved for PROJECT
    before = len(app.store.writes)
    with pytest.raises(WriteNotAuthorized):
        ctx.testing.run(env.id, "acme_test", health=False, set_id=a.requirement_set.id, behavioural=True)
    assert len(app.store.writes) == before
    # and the transport itself refuses a write aimed at another project, whatever the caller says
    session = ctx.connections.sync_environment(env.id)
    auth = session.guard.issue(PROJECT, "probe", "tester")
    with pytest.raises(WriteNotAuthorized, match="only allowed in the authorized test project"):
        session.rest.send("POST", "/projects/acme_test/workitems", json={"data": []}, write_auth=auth)
    ctx.close()


def test_production_needs_the_environment_name_typed(world):
    ctx, app, env = world(kind=EnvironmentKind.PRODUCTION)
    a = ctx.testing.import_pasted("S-3: Task work items can move from Open to Done.", "s", env.id, PROJECT)
    with pytest.raises(WriteNotAuthorized, match="production"):
        ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    assert app.store.writes == []
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True,
                          production_confirmation=env.name)
    assert any(x.status == S.PASS for x in run.results) and store_ids(app) == SEED_IDS
    ctx.close()


def test_cleanup_runs_even_when_the_run_crashes(world, monkeypatch):
    import pavs.services.testing as svc
    ctx, app, env = world()
    a = ctx.testing.import_pasted("S-4: Task work items can move from Open to Done.", "s", env.id, PROJECT)

    def crash(snap, **kw):
        beh = kw["behaviour"]
        ref, err = beh.create_item(beh.admin, "task", "about to crash")
        assert ref, err
        raise RuntimeError("boom")
    monkeypatch.setattr(svc, "run_checks", crash)
    with pytest.raises(RuntimeError, match="boom"):
        ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    assert store_ids(app) == SEED_IDS and not ctx.testing.leftover_artifacts(env.id)
    assert "behavioural.cleanup" in [x["action"] for x in ctx.audit.recent()]
    ctx.close()


def test_leftovers_from_an_earlier_crash_can_be_cleaned_up_later(world):
    ctx, app, env = world()
    session = ctx.connections.sync_environment(env.id)
    from pavs.engines.testing.behaviour import Behaviour
    from pavs.engines.testing.view import ConfigView
    from pavs.domain.config import ConfigSnapshot
    auth = session.guard.issue(PROJECT, "oldrun1", "tester")
    beh = Behaviour(session=session, project=PROJECT, run_id="oldrun1", env_id=env.id, auth=auth,
                    view=ConfigView(ConfigSnapshot(environment_id=env.id, project_id=PROJECT, release=None, adapter="")),
                    accounts={}, artifacts=ctx.testing.artifacts)
    assert beh.create_item(beh.admin, "task", "left behind")[0]               # ... and the process "dies" here
    assert len(ctx.testing.leftover_artifacts(env.id)) == 1 and len(app.store.items[PROJECT]) == len(SEED_IDS[PROJECT]) + 1
    deleted, leftovers, log = ctx.testing.cleanup(env.id, PROJECT)
    assert deleted == 1 and leftovers == [] and store_ids(app) == SEED_IDS
    assert ctx.testing.cleanup(env.id, PROJECT)[2] == ["Nothing to clean up."]
    ctx.close()


def test_cleanup_never_deletes_an_item_it_did_not_tag(world):
    """If an artifact's Work Item no longer carries the test tag (someone edited it), it is left alone."""
    from pavs.domain.testing import TestArtifact
    ctx, app, env = world()
    ctx.testing.artifacts.add(TestArtifact(run_id="x1", environment_id=env.id, project_id=PROJECT, ref="AD-4", title="[PAVS-x1] fake"))
    deleted, leftovers, log = ctx.testing.cleanup(env.id, PROJECT)
    assert deleted == 0 and leftovers == ["AD-4"] and "AD-4" in app.store.items[PROJECT]
    assert any("NOT deleted" in l for l in log)
    ctx.close()


def test_item_cap_per_run(world):
    ctx, app, env = world()
    session = ctx.connections.sync_environment(env.id)
    from pavs.engines.testing.behaviour import Behaviour
    from pavs.engines.testing.view import ConfigView
    from pavs.domain.config import ConfigSnapshot
    auth = session.guard.issue(PROJECT, "cap1", "tester")
    beh = Behaviour(session=session, project=PROJECT, run_id="cap1", env_id=env.id, auth=auth, max_items=3,
                    view=ConfigView(ConfigSnapshot(environment_id=env.id, project_id=PROJECT, release=None, adapter="")),
                    accounts={}, artifacts=ctx.testing.artifacts)
    made = [beh.create_item(beh.admin, "task", f"n{i}")[0] for i in range(5)]
    assert made[:3] != [None] * 3 and made[3:] == [None, None]
    beh.cleanup()
    assert store_ids(app) == SEED_IDS
    ctx.close()


def test_without_rest_behavioural_tests_are_blocked_and_nothing_is_written(tmp_path):
    import httpx
    app = MockPolarion(PROFILES["2410-rest-off"])
    ctx = build_context(db_path=tmp_path / "o.sqlite3", vault_backend=MemoryBackend(),
                        http_transport=httpx.WSGITransport(app=app), log_to_file=False)
    out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw")
    env = ctx.environments.allow_writes_in(out.environment.id, PROJECT, "tester")
    a = ctx.testing.import_pasted("S-5: Task work items can move from Open to Done.", "s", env.id, PROJECT)
    run = ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    b00 = next(x for x in run.results if x.check_id == "B00")
    assert b00.status == S.BLOCKED and "REST" in " ".join(b00.evidence)
    assert app.store.writes == []
    ctx.close()


def test_approval_and_runs_are_audited(world):
    ctx, app, env = world()
    a = ctx.testing.import_pasted("S-6: Task work items can move from Open to Done.", "s", env.id, PROJECT)
    ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    actions = [x["action"] for x in ctx.audit.recent(200)]
    for needed in ("writes.allow_project", "write.authorize", "behavioural.start", "behavioural.cleanup", "tests.run"):
        assert needed in actions, needed
    assert "test-token-tester" not in str(ctx.audit.recent(200))
    ctx.environments.revoke_writes_in(env.id, PROJECT)
    with pytest.raises(WriteNotAuthorized):
        ctx.testing.run(env.id, PROJECT, health=False, set_id=a.requirement_set.id, behavioural=True)
    ctx.close()


def test_test_account_tokens_are_not_stored_in_the_database(world):
    ctx, app, env = world()
    rows = " ".join(r["data"] for r in ctx.db.query("SELECT data FROM environments"))
    assert "test-token-tester" not in rows and '"internal_reviewer":"tester1"' in rows.replace(" ", "")
    ctx.close()
