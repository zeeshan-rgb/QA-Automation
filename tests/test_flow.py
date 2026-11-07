"""The user's flow: URL + credentials -> projects -> tests -> report. Service level, mock server."""
import httpx
import pytest
from openpyxl import load_workbook

from pavs.app import build_context
from pavs.domain.errors import ConfigurationError
from pavs.infra.vault import MemoryBackend

URL = "https://mock.test/polarion"
REQS = """REQ-1: Create a role called Internal Reviewer. The role should have permission to review Work Items but should not have permission to modify or delete them.
REQ-2: The field Reviewer is mandatory for Requirement work items.
REQ-3: Requirement work items can move from Draft to Approved.
REQ-4: The system shall be fast.
"""


@pytest.fixture
def ctx(tmp_path, mock_server):
    def make(profile="2410-rest-off"):
        _, wsgi = mock_server(profile)
        return build_context(db_path=tmp_path / "f.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi,
                             log_to_file=False)
    c = make()
    yield c
    c.close()


def test_connect_with_password_lists_projects(ctx):
    out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw")
    assert out.ok and "Polarion 2410" in out.message and "admin" in out.message
    assert [p.id for p in out.profile.projects] == ["acme_demo", "acme_test"]
    kinds = {p.kind.value: p.state.value for p in out.profile.providers}
    assert kinds["soap"] == "authenticated" and kinds["svn"] == "authenticated" and kinds["rest"] == "disabled"


def test_connect_with_token_only(ctx, mock_server):
    ctx2 = ctx
    out = ctx2.connections.quick_connect(URL, token="test-token-admin", read_config_files=False)
    assert out.ok and out.profile.provider(__import__("pavs.domain.enums", fromlist=["x"]).ProviderKind.SVN) is None


def test_wrong_password_gives_a_clear_message(ctx):
    out = ctx.connections.quick_connect(URL, "admin", "wrong-password")
    assert not out.ok and "Sign-in failed" in out.message and "wrong-password" not in out.message


def test_missing_credentials_and_bad_url(ctx):
    assert "username and password" in ctx.connections.quick_connect(URL, "admin", "").message
    out = ctx.connections.quick_connect("not a url", "admin", "x")
    assert not out.ok and "Check the connection details" in out.message


def test_unreachable_server_is_explained(tmp_path):
    def refuse(req):
        raise httpx.ConnectError("connection refused")
    c = build_context(db_path=tmp_path / "u.sqlite3", vault_backend=MemoryBackend(),
                      http_transport=httpx.MockTransport(refuse), log_to_file=False)
    out = c.connections.quick_connect(URL, "admin", "pw")
    assert not out.ok and "Cannot reach the server" in out.message
    c.close()


def test_credentials_are_only_kept_when_asked(ctx):
    out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw", remember=False)
    env_id = out.environment.id
    assert ctx.environments.credentials(env_id).password == "admin-test-pw"        # this session only
    rows = ctx.db.query("SELECT data FROM environments")
    assert "admin-test-pw" not in rows[0]["data"]
    # same URL again reuses the saved connection instead of creating a duplicate
    ctx.connections.quick_connect(URL, "admin", "admin-test-pw")
    assert len(ctx.environments.list()) == 1


def test_full_run_report_and_history(ctx, tmp_path):
    out = ctx.connections.quick_connect(URL, "admin", "admin-test-pw")
    env_id = out.environment.id
    a = ctx.testing.import_pasted(REQS, "Example", env_id, "acme_demo")
    assert len(a.requirements) == 4 and len(a.cases) == 7
    assert any(f.kind.value == "ambiguous" for f in a.findings)

    # unticking a test excludes it from the run and the choice survives a re-analysis
    skipped = next(c for c in a.cases if "delete" in c.title)
    ctx.testing.set_case_enabled(skipped.id, a.requirement_set.id, False)
    ctx.testing.analyze(a.requirement_set.id, env_id, "acme_demo")
    assert [c.status.value for c in ctx.testing.load(a.requirement_set.id).cases if "delete" in c.title] == ["rejected"]

    seen = []
    run = ctx.testing.run(env_id, "acme_demo", health=True, set_id=a.requirement_set.id,
                          progress=lambda m, p: seen.append(p))
    assert seen and seen[-1] == 100 and seen == sorted(seen)
    assert run.total == 14 + 6                                  # 14 health + 6 enabled requirement checks
    req = {r.name: r for r in run.results if r.group == "Requirements"}
    assert not [n for n in req if "delete" in n]
    approved = next(r for n, r in req.items() if "can move" in n)
    assert approved.status.value == "fail" and run.verdict == "FAILED"
    assert any(r.status.value == "pass" and r.mechanism == "SOAP Security.hasPermission" for r in run.results)

    assert ctx.testing.history(env_id, "acme_demo")[0].id == run.id
    assert ctx.testing.get_run(run.id).total == run.total

    x = ctx.testing.export(run.id, tmp_path / "report.xlsx")
    wb = load_workbook(x)
    assert wb.sheetnames == ["Summary", "Results"] and wb["Results"].max_row == run.total + 1
    assert "FAILED" in [c.value for row in wb["Summary"].iter_rows() for c in row]
    html = ctx.testing.export(run.id, tmp_path / "report.html").read_text(encoding="utf-8")
    csv = ctx.testing.export(run.id, tmp_path / "report.csv").read_text(encoding="utf-8-sig")
    for text in (html, csv):
        assert "admin-test-pw" not in text and "test-token-admin" not in text
    assert "FAIL" in html and "executed check" in html.lower() and "no ai" not in html.lower()
    pdf = ctx.testing.export(run.id, tmp_path / "report.pdf")
    raw = pdf.read_bytes()
    assert raw.startswith(b"%PDF") and len(raw) > 2000
    from pypdf import PdfReader
    text = " ".join(pg.extract_text() or "" for pg in PdfReader(str(pdf)).pages)
    assert "FAILED" in text and "admin-test-pw" not in text and "test-token-admin" not in text
    with pytest.raises(ValueError):
        ctx.testing.export(run.id, tmp_path / "report.docx")


def test_health_only_and_validation(ctx):
    env_id = ctx.connections.quick_connect(URL, "admin", "admin-test-pw").environment.id
    run = ctx.testing.run(env_id, "acme_demo", health=True)
    assert run.kind == "health" and run.total == 14 and run.verdict in ("PASSED", "PASSED WITH GAPS")
    with pytest.raises(ConfigurationError):
        ctx.testing.run(env_id, "acme_demo", health=False, set_id=None)
    with pytest.raises(ConfigurationError):
        ctx.testing.import_pasted("   ", "x")


def test_test_runs_never_write_to_polarion(ctx, mock_server):
    """Phase 3 is read-only: no POST that changes data, no PATCH/PUT/DELETE reaches the server."""
    app, wsgi = mock_server("2410-rest-off")
    c = build_context(db_path=":memory:", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    env = c.connections.quick_connect(URL, "admin", "admin-test-pw").environment
    a = c.testing.import_pasted(REQS, "x", env.id, "acme_demo")
    c.testing.run(env.id, "acme_demo", health=True, set_id=a.requirement_set.id)
    assert {m for m, _ in app.requests} <= {"GET", "POST", "PROPFIND"}
    posts = {p for m, p in app.requests if m == "POST"}
    assert all(p.startswith("/polarion/ws/services/") for p in posts)       # SOAP reads only
    c.close()
