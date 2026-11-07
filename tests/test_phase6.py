"""Phase 5/6 additions: headless CLI, JWT bearer login, PDF/roles are covered in their own files."""
import json

import pytest

from mock_polarion.profiles import MockUser
from pavs import cli
from pavs.app import build_context
from pavs.domain.enums import ProviderKind, ProviderState
from pavs.infra.vault import MemoryBackend
from pavs.polarion.auth import looks_like_jwt
from pavs.services.roles import AppRole

URL = "https://mock.test/polarion"
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJqd3R1c2VyIn0.c2lnbmF0dXJlLXZhbHVl"


@pytest.fixture
def env_creds(monkeypatch):
    monkeypatch.setenv("PAVS_USER", "admin")
    monkeypatch.setenv("PAVS_PASSWORD", "admin-test-pw")
    monkeypatch.setenv("PAVS_TOKEN", "test-token-admin")


@pytest.fixture
def ctx(tmp_path, mock_server):
    _, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "cli.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    yield c
    c.close()


# ---------------------------------------------------------------- JWT
def test_jwt_detection():
    assert looks_like_jwt(FAKE_JWT)
    assert not looks_like_jwt("test-token-admin") and not looks_like_jwt("") and not looks_like_jwt(None)


def test_jwt_is_used_for_rest_only(tmp_path, mock_server):
    app, wsgi = mock_server("2606")
    app.p.users.append(MockUser("jwtuser", "SSO user", password="x", token=FAKE_JWT))
    c = build_context(db_path=tmp_path / "jwt.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    out = c.connections.quick_connect(URL, "", "", token=FAKE_JWT)
    prof = out.profile
    assert prof.provider(ProviderKind.REST).state == ProviderState.AUTHENTICATED
    assert "JWT" in prof.provider(ProviderKind.REST).detail
    soap = prof.provider(ProviderKind.SOAP)
    assert soap.state != ProviderState.AUTHENTICATED and "JWT works for REST only" in soap.detail
    c.close()


# ---------------------------------------------------------------- CLI
def test_cli_run_writes_report_and_sets_exit_code(ctx, env_creds, tmp_path, capsys):
    report = tmp_path / "out.html"
    code = cli.main(["run", "--url", URL, "--project", "acme_demo", "--data", "--scripts", "--out", str(report)], ctx=ctx)
    out = capsys.readouterr().out
    assert code == cli.EXIT_FAILED                                  # the mock repo has a real Velocity error (V01)
    assert "FAILED" in out and "Report saved" in out and report.exists()
    assert "admin-test-pw" not in out and "test-token-admin" not in out
    code2 = cli.main(["run", "--url", URL, "--project", "acme_demo"], ctx=ctx)
    assert code2 in (cli.EXIT_OK, cli.EXIT_FAILED)
    assert "Since the previous run" in capsys.readouterr().out


def test_cli_scripts_offline(tmp_path, capsys):
    (tmp_path / "bad.js").write_text('var password = "hunter22x";')
    c = build_context(db_path=tmp_path / "x.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    pdf = tmp_path / "scripts.pdf"
    code = cli.main(["scripts", str(tmp_path), "--release", "2410", "--out", str(pdf)], ctx=c)
    assert code == cli.EXIT_FAILED and pdf.read_bytes().startswith(b"%PDF")
    assert "hunter22x" not in capsys.readouterr().out
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "ok.js").write_text("var a = 1;")
    assert cli.main(["scripts", str(clean)], ctx=c) == cli.EXIT_OK
    c.close()


def test_cli_projects_and_compat_record(ctx, env_creds, tmp_path, capsys):
    assert cli.main(["projects", "--url", URL], ctx=ctx) == cli.EXIT_OK
    assert "acme_demo" in capsys.readouterr().out
    md, js = tmp_path / "compat.md", tmp_path / "compat.json"
    assert cli.main(["compat", "--url", URL, "--out", str(md)], ctx=ctx) == cli.EXIT_OK
    assert cli.main(["compat", "--url", URL, "--out", str(js)], ctx=ctx) == cli.EXIT_OK
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["release"] == "2606" and data["capabilities"] and "verified" in {c["verification"] for c in data["capabilities"]}
    text = md.read_text(encoding="utf-8")
    assert "| Area | Capability |" in text and "test-token-admin" not in text


def test_cli_errors_are_reported_not_raised(ctx, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PAVS_USER", raising=False)
    monkeypatch.delenv("PAVS_PASSWORD", raising=False)
    monkeypatch.delenv("PAVS_TOKEN", raising=False)
    assert cli.main(["projects", "--url", URL], ctx=ctx) == cli.EXIT_ERROR
    assert "Enter a username and password" in capsys.readouterr().err
    assert cli.main(["scripts", str(tmp_path / "missing")], ctx=ctx) in (cli.EXIT_OK, cli.EXIT_ERROR)


def test_cli_respects_the_application_role(ctx, env_creds, capsys):
    ctx.roles.set_role(AppRole.VIEWER)
    assert cli.main(["run", "--url", URL, "--project", "acme_demo"], ctx=ctx) == cli.EXIT_ERROR
    assert "Not allowed" in capsys.readouterr().err


def test_cli_merge_check(ctx, env_creds, tmp_path, capsys):
    out = tmp_path / "merge.pdf"
    code = cli.main(["merge-check", "--url", URL, "--project", "acme_demo", "--target-project", "acme_test", "--out", str(out)], ctx=ctx)
    text = capsys.readouterr().out
    assert code == cli.EXIT_FAILED and "WOULD BREAK" in text and "render.vm" in text     # the incoming Velocity template has a real V01 error
    assert out.read_bytes().startswith(b"%PDF") and "admin-test-pw" not in text
    assert "Two-way comparison" in text


def test_separate_repository_login(tmp_path, mock_server):
    """/repo may use a different login than the Polarion login: the optional repository user/password are used for SVN only."""
    from pavs.domain.enums import ProviderKind, ProviderState
    app, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "svn.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    # wrong repository login: SVN fails, SOAP (Polarion login) still works
    out = c.connections.quick_connect(URL, "admin", "admin-test-pw", svn_user="admin", svn_password="not-the-repo-password")
    assert out.profile.provider(ProviderKind.SOAP).state == ProviderState.AUTHENTICATED
    assert out.profile.provider(ProviderKind.SVN).state == ProviderState.AUTH_FAILED
    assert "separate" in out.profile.provider(ProviderKind.SVN).detail
    # right repository login
    out = c.connections.quick_connect(URL, "admin", "admin-test-pw", svn_user="admin", svn_password="admin-test-pw")
    assert out.profile.provider(ProviderKind.SVN).state == ProviderState.AUTHENTICATED
    # the repository password is registered for redaction and never appears in the stored profile
    from pavs.infra.redaction import scrub
    assert "not-the-repo-password" not in (scrub("x not-the-repo-password y") or "")
    c.close()
