"""Merge preparation (read-only compare + incoming checks) and the TortoiseSVN hand-off."""
from pathlib import Path

import pytest

from mock_polarion import repo_fixture
from pavs.app import build_context
from pavs.domain.enums import EnvironmentKind
from pavs.domain.errors import ConfigurationError
from pavs.infra.vault import MemoryBackend
from pavs.integrations import tortoisesvn
from pavs.integrations.tortoisesvn import TortoiseInfo, TortoiseLauncher, clean_path, clean_url
from pavs.services.roles import AppRole, RoleDenied

from .conftest import SPAWNED

URL = "https://mock.test/polarion"
T = repo_fixture.T
P = repo_fixture.P
FAKE_PROC = Path("C:/Program Files/TortoiseSVN/bin/TortoiseProc.exe")


@pytest.fixture
def ctx(tmp_path, mock_server, monkeypatch):
    _, wsgi = mock_server("2606")
    files = repo_fixture.FILES
    same = files[f"{P}/tracker/fields/requirement-status-enum.xml"]
    monkeypatch.setitem(files, f"{T}/tracker/fields/requirement-status-enum.xml", same)                    # identical in both
    monkeypatch.setitem(files, f"{T}/tracker/workflow/requirement-workflow.xml",
                        files[f"{P}/tracker/workflow/requirement-workflow.xml"].replace("rework", "rework2"))   # differs
    monkeypatch.setitem(files, f"{T}/security/only-in-target.xml", "<x/>")                                  # only in the target
    monkeypatch.setitem(files, f"{P}/tracker/fields/broken.xml", "<fields><field></fields>")               # incoming, not well-formed
    launcher = TortoiseLauncher(TortoiseInfo(proc=FAKE_PROC, merge=None))
    c = build_context(db_path=tmp_path / "m.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi,
                      log_to_file=False, svn_launcher=launcher)
    out = c.connections.quick_connect(URL, "admin", "admin-test-pw", token="test-token-admin")
    assert out.ok, out.message
    yield c, out.environment
    c.close()


def test_compare_classifies_files_and_blocks_on_broken_incoming(ctx):
    c, env = ctx
    rep = c.merge.compare(env.id, "acme_demo", env.id, "acme_test")
    by = {f.path: f for f in rep.files}
    assert by["tracker/fields/broken.xml"].state == "added"
    assert [x.rule for x in by["tracker/fields/broken.xml"].findings] == ["M01"]
    assert by["tracker/workflow/requirement-workflow.xml"].state == "changed"
    assert by["security/only-in-target.xml"].state == "removed"
    assert "tracker/fields/requirement-status-enum.xml" not in by                  # identical files are hidden
    assert by["pages/widgets/demo/render.vm"].state == "added"
    assert any(x.rule == "V01" for x in by["pages/widgets/demo/render.vm"].findings)
    assert not rep.ready and len(rep.blockers) >= 2
    assert rep.counts()["removed"] == 1
    assert rep.source_root_url.endswith("/repo/Acme/acme_demo/.polarion") and rep.target_root_url.endswith("/repo/acme_test/.polarion")
    assert any("Two-way" in n for n in rep.notes)


def test_report_is_saved_and_exportable(ctx, tmp_path):
    c, env = ctx
    rep = c.merge.compare(env.id, "acme_demo", env.id, "acme_test")
    run = c.testing.get_run(rep.run_id)
    assert run.kind == "merge" and run.verdict == "FAILED" and run.project_id == "acme_demo -> acme_test"
    assert any(r.name == "tracker/fields/broken.xml" and r.status.value == "fail" for r in run.results)
    assert any(r.name == "security/only-in-target.xml" and r.status.value == "not_testable" for r in run.results)
    pdf = c.testing.export(rep.run_id, tmp_path / "merge.pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert c.testing.export(rep.run_id, tmp_path / "merge.xlsx").exists()


def test_compare_needs_two_different_projects_and_a_known_project(ctx):
    c, env = ctx
    with pytest.raises(ConfigurationError, match="same project"):
        c.merge.compare(env.id, "acme_demo", env.id, "acme_demo")
    with pytest.raises(ConfigurationError, match="not found in the repository"):
        c.merge.compare(env.id, "acme_demo", env.id, "ghost")


def test_nothing_is_written_to_the_repository(ctx):
    c, env = ctx
    before = dict(repo_fixture.FILES)
    c.merge.compare(env.id, "acme_demo", env.id, "acme_test")
    assert repo_fixture.FILES == before


# ---------------------------------------------------------------- TortoiseSVN hand-off (commands are recorded, never started)
def test_launcher_builds_documented_command_lines(tmp_path):
    t = TortoiseLauncher(TortoiseInfo(proc=FAKE_PROC, merge=None))
    line = t.compare_urls("https://h/repo/Acme/a b/.polarion", "https://h/repo/x/.polarion")
    assert line.startswith('"C:') and "/command:showcompare" in line
    assert '/url1:"https://h/repo/Acme/a%20b/.polarion"' in line and '/revision1:"HEAD"' in line
    wc = tmp_path / "wc"
    wc.mkdir()
    assert f'/command:merge /path:"{wc}"' in t.merge(wc)
    assert "/command:checkout" in t.checkout("https://h/repo/x/.polarion", wc) and "/url:" in SPAWNED[-1]
    assert "/command:log" in t.log("https://h/repo/x/page.xml")
    assert len(SPAWNED) == 4                                                          # recorded, none started


def test_launcher_refuses_unsafe_values(tmp_path):
    for bad in ('https://h/x" /command:remove', "javascript:alert(1)", "ftp://h/x", "not a url", "https://h/x\r\n/command:commit"):
        with pytest.raises(ConfigurationError):
            clean_url(bad)
    with pytest.raises(ConfigurationError):
        clean_path(str(tmp_path / "missing"))
    with pytest.raises(ConfigurationError):
        clean_path('C:\\a" /command:remove', must_exist=False)


def test_missing_tortoisesvn_is_explained(monkeypatch):
    monkeypatch.setattr(tortoisesvn, "find_tortoise", lambda: None)
    t = TortoiseLauncher()
    assert not t.available
    with pytest.raises(ConfigurationError, match="TortoiseSVN was not found"):
        t.log("https://h/repo/x")
    assert not SPAWNED


def test_no_password_ever_reaches_a_command_line(ctx, tmp_path):
    c, env = ctx
    rep = c.merge.compare(env.id, "acme_demo", env.id, "acme_test")
    wc = tmp_path / "wc"
    wc.mkdir()
    c.merge.open_compare(rep)
    c.merge.checkout_target(rep, wc)
    c.merge.open_merge(wc, env.id)
    text = " ".join(SPAWNED)
    assert "admin-test-pw" not in text and "test-token-admin" not in text and "admin:" not in text
    assert len(SPAWNED) == 3
    started = [r["action"] for r in c.audit.recent() if r["action"].startswith("tortoisesvn.")]
    assert started[:3] == ["tortoisesvn.merge", "tortoisesvn.checkout", "tortoisesvn.compare"]


def test_roles_gate_the_hand_offs(ctx, tmp_path):
    c, env = ctx
    rep = c.merge.compare(env.id, "acme_demo", env.id, "acme_test")
    wc = tmp_path / "wc"
    wc.mkdir()
    c.roles.set_role(AppRole.TESTER)
    c.merge.open_compare(rep)                                                         # looking is fine for a Tester
    with pytest.raises(RoleDenied, match="Lead"):
        c.merge.checkout_target(rep, wc)
    with pytest.raises(RoleDenied):
        c.merge.open_merge(wc, env.id)
    c.roles.set_role(AppRole.VIEWER)
    with pytest.raises(RoleDenied):
        c.merge.compare(env.id, "acme_demo", env.id, "acme_test")


def test_production_target_needs_admin(ctx, tmp_path):
    c, env = ctx
    c.environments.save(env.model_copy(update={"kind": EnvironmentKind.PRODUCTION}))
    wc = tmp_path / "wc"
    wc.mkdir()
    c.roles.set_role(AppRole.LEAD)
    with pytest.raises(RoleDenied, match="Admin"):
        c.merge.open_merge(wc, env.id)
    c.roles.set_role(AppRole.ADMIN)
    c.merge.open_merge(wc, env.id)
    assert SPAWNED
