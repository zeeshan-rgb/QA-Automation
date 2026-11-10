import sqlite3

import pytest

from pavs.app import build_context
from pavs.domain.errors import ConfigurationError, CredentialError
from pavs.domain.models import Credentials, Environment
from pavs.infra.storage import AuditLog, Database
from pavs.infra.vault import CredentialVault, MemoryBackend

from .conftest import ADMIN_TOKEN, make_env


@pytest.fixture
def ctx(tmp_path, mock_server):
    _, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "t.sqlite3", vault_backend=MemoryBackend(),
                      http_transport=wsgi, log_to_file=False)
    yield c
    c.close()


def test_environment_url_normalisation():
    assert Environment(name="a", base_url="https://h.example/").base_url == "https://h.example/polarion"
    assert Environment(name="a", base_url="https://h.example/polarion/").server_root == "https://h.example"
    with pytest.raises(ValueError):
        Environment(name="a", base_url="h.example")


def test_save_list_delete_and_credentials(ctx):
    env = ctx.environments.save(make_env(name="Dev"), ADMIN_TOKEN)
    assert [e.name for e in ctx.environments.list()] == ["Dev"]
    assert ctx.environments.has_credentials(env.id) == {"token": True, "password": False, "svn_password": False}
    # updating without a new secret keeps the stored one
    ctx.environments.save(env.model_copy(update={"notes": "x"}), Credentials())
    assert ctx.environments.credentials(env.id).token == "test-token-admin"
    with pytest.raises(ConfigurationError):
        ctx.environments.save(make_env(name="dev"))
    ctx.environments.delete(env.id)
    assert ctx.environments.list() == []
    assert ctx.environments.credentials(env.id) is None


def test_environment_json_has_no_secret(ctx):
    env = ctx.environments.save(make_env(name="Dev"), ADMIN_TOKEN)
    rows = ctx.db.query("SELECT data FROM environments WHERE id=?", (env.id,))
    assert "test-token-admin" not in rows[0]["data"]


def test_connect_stores_profile_and_audit(ctx):
    env = ctx.environments.save(make_env(name="Dev"), ADMIN_TOKEN)
    profile = ctx.connections.connect(env)
    assert profile.version.release == "2606"
    assert ctx.connections.latest_profile(env.id).version.release == "2606"
    actions = [a["action"] for a in ctx.audit.recent()]
    assert "environment.connect" in actions and "environment.create" in actions
    assert "test-token-admin" not in str(ctx.audit.recent())


def test_audit_log_is_append_only(tmp_path):
    db = Database(tmp_path / "a.sqlite3")
    AuditLog(db, "tester").record("x", "ok")
    with pytest.raises(sqlite3.DatabaseError):
        db.query("UPDATE audit_log SET outcome='changed'")
    with pytest.raises(sqlite3.DatabaseError):
        db.query("DELETE FROM audit_log")
    from pavs.infra.storage import MIGRATIONS
    assert db.schema_version == len(MIGRATIONS)


def test_vault_refuses_insecure_backend():
    class Insecure(MemoryBackend):
        @property
        def secure(self):
            return False
    with pytest.raises(CredentialError):
        CredentialVault(Insecure()).save("k", Credentials(token="abcd1234"))


def test_existing_database_is_upgraded_in_place_without_losing_data(tmp_path):
    """A user who ran an older version keeps saved connections and history when the schema gains new tables."""
    import sqlite3
    from pavs.infra.storage import MIGRATIONS, EnvironmentRepository

    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:3], start=1):          # what Phase 3 created
        raw.executescript("BEGIN;" + script + f"PRAGMA user_version={version}; COMMIT;")
    env = make_env(name="Saved before the upgrade")
    raw.execute("INSERT INTO environments(id, name, data, updated_at) VALUES(?,?,?,?)",
                (env.id, env.name, env.model_dump_json(exclude={"test_accounts"}), "2026-11-06"))   # old JSON: no test_accounts
    raw.commit()
    raw.close()

    db = Database(path)
    assert db.schema_version == len(MIGRATIONS) == 6
    saved = EnvironmentRepository(db).get(env.id)
    assert saved.name == "Saved before the upgrade" and saved.test_accounts == {}
    assert db.query("SELECT COUNT(*) AS n FROM test_artifacts")[0]["n"] == 0
    db.close()


def test_a_pasted_page_address_is_reduced_to_the_server_address():
    from pavs.domain.models import Environment, split_polarion_url
    cases = {
        "http://localhost/polarion/#/project/Vision/home": ("http://localhost/polarion", "Vision"),
        "http://localhost/polarion/": ("http://localhost/polarion", None),
        "https://h.example.com/polarion/polarion/#/project/My%20Proj/wiki/Spec": ("https://h.example.com/polarion", "My Proj"),
        "https://h.example.com/polarion/login?x=1": ("https://h.example.com/polarion", None),
        "https://h.example.com": ("https://h.example.com/polarion", None),
        "https://h.example.com/#/project/Vision": ("https://h.example.com/polarion", None),
        "http://localhost/polarion/#/project/Vision/home/polarion": ("http://localhost/polarion", "Vision"),
    }
    for given, (base, project) in cases.items():
        assert split_polarion_url(given) == (base, project), given
        assert Environment(name="x", base_url=given).base_url == base
    with pytest.raises(ValueError):
        split_polarion_url("localhost/polarion")
