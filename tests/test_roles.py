"""Application roles: Viewer < Tester < Lead < Admin, enforced in the services."""
import pytest

from pavs.app import build_context
from pavs.domain.enums import EnvironmentKind
from pavs.infra.vault import MemoryBackend
from pavs.services.roles import Action, AppRole, RoleDenied

from .conftest import ADMIN_TOKEN, make_env

URL = "https://mock.test/polarion"


@pytest.fixture
def ctx(tmp_path, mock_server):
    _, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "roles.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    yield c
    c.close()


def test_first_run_role_is_admin_and_survives_restart(ctx, tmp_path):
    assert ctx.roles.current == AppRole.ADMIN
    ctx.roles.set_role(AppRole.TESTER)
    again = build_context(db_path=tmp_path / "roles.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    assert again.roles.current == AppRole.TESTER
    again.close()


def test_action_matrix(ctx):
    expected = {
        AppRole.VIEWER: {Action.VIEW},
        AppRole.TESTER: {Action.VIEW, Action.RUN_TESTS},
        AppRole.LEAD: {Action.VIEW, Action.RUN_TESTS, Action.WRITE_TESTS, Action.APPROVE_WRITES, Action.SVN_WRITE_TOOLS},
        AppRole.ADMIN: set(Action),
    }
    for role, allowed in expected.items():
        ctx.roles.set_role(role)
        assert {a for a in Action if ctx.roles.allows(a)} == allowed, role


def test_viewer_cannot_connect_or_run(ctx):
    env = ctx.environments.save(make_env(name="V"), ADMIN_TOKEN)
    ctx.roles.set_role(AppRole.VIEWER)
    with pytest.raises(RoleDenied, match="Tester"):
        ctx.connections.connect(env)
    with pytest.raises(RoleDenied):
        ctx.testing.run(env.id, "acme_demo")
    with pytest.raises(RoleDenied):
        ctx.testing.analyze_local_scripts(".", "2410")
    assert ctx.testing.history() == []                      # viewing stays allowed


def test_tester_runs_read_only_tests_but_not_behavioural(ctx):
    out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw", token="test-token-admin")
    assert out.ok
    ctx.roles.set_role(AppRole.TESTER)
    run = ctx.testing.run(out.environment.id, "acme_demo", health=True)
    assert run.total > 0
    with pytest.raises(RoleDenied, match="Lead"):
        ctx.testing.run(out.environment.id, "acme_demo", health=True, behavioural=True)
    with pytest.raises(RoleDenied):
        ctx.testing.cleanup(out.environment.id, "acme_demo")


def test_environment_changes(ctx):
    env = ctx.environments.save(make_env(name="Dev", kind=EnvironmentKind.DEVELOPMENT, test_projects=[]), ADMIN_TOKEN)
    ctx.roles.set_role(AppRole.TESTER)
    ctx.environments.save(env.model_copy(update={"base_url": "https://other.test/polarion"}))              # plain edit: fine
    with pytest.raises(RoleDenied, match="Lead"):
        ctx.environments.save(env.model_copy(update={"test_projects": ["acme_test"]}))                        # approving writes needs Lead
    with pytest.raises(RoleDenied, match="Admin"):
        ctx.environments.save(make_env(name="Prod", kind=EnvironmentKind.PRODUCTION, test_projects=[]), ADMIN_TOKEN)
    with pytest.raises(RoleDenied, match="Admin"):
        ctx.environments.delete(env.id)
    ctx.roles.set_role(AppRole.LEAD)
    ctx.environments.save(env.model_copy(update={"test_projects": ["acme_test"]}))
    ctx.roles.set_role(AppRole.ADMIN)
    ctx.environments.delete(env.id)


def test_raising_the_role_needs_the_passphrase_once_one_is_set(ctx):
    ctx.roles.set_passphrase("correct horse")
    assert ctx.roles.passphrase_set
    ctx.roles.set_role(AppRole.VIEWER)                                       # lowering is free
    with pytest.raises(RoleDenied, match="passphrase"):
        ctx.roles.set_role(AppRole.ADMIN, "wrong")
    assert ctx.roles.current == AppRole.VIEWER
    ctx.roles.set_role(AppRole.ADMIN, "correct horse")
    assert ctx.roles.current == AppRole.ADMIN
    with pytest.raises(RoleDenied):
        ctx.roles.set_passphrase("new passphrase", old="nope")
    with pytest.raises(RoleDenied, match="6 characters"):
        ctx.roles.set_passphrase("abc", old="correct horse")
    ctx.roles.set_passphrase("", old="correct horse")                         # clear it
    assert not ctx.roles.passphrase_set


def test_passphrase_is_stored_hashed_and_denials_are_audited(ctx):
    ctx.roles.set_passphrase("correct horse")
    stored = ctx.db.query("SELECT value FROM app_settings WHERE key='role_passphrase'")[0][0]
    assert "correct horse" not in stored and "$" in stored
    ctx.roles.set_role(AppRole.VIEWER)
    with pytest.raises(RoleDenied):
        ctx.roles.require(Action.RUN_TESTS)
    actions = [r["action"] for r in ctx.audit.recent()]
    assert "role.denied" in actions and "role.change" in actions


def test_a_damaged_role_value_fails_closed(ctx):
    with ctx.db.tx() as c:
        c.execute("INSERT INTO app_settings(key, value) VALUES('app_role', 'superuser')")
    assert ctx.roles.current == AppRole.VIEWER
