"""Reading a local Polarion repository with svnlook (no repository login). Needs Subversion's command line tools."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pavs.app import build_context
from pavs.domain.enums import ProviderKind, ProviderState
from pavs.domain.errors import NotFound
from pavs.infra.vault import MemoryBackend
from pavs.polarion import connection
from pavs.polarion.svn.local import LocalSvnReader, find_svnlook, is_repository

TOOLS = Path("E:/bundled/svn/bin")


def _tool(name):
    for c in (os.environ.get("PAVS_SVN_TOOLS"), str(TOOLS)):
        if c and (Path(c) / f"{name}.exe").is_file():
            return str(Path(c) / f"{name}.exe")
    return shutil.which(name)


needs_svn = pytest.mark.skipif(not (_tool("svnlook") and _tool("svnadmin") and _tool("svn")), reason="Subversion tools not installed")


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    subprocess.run([_tool("svnadmin"), "create", str(repo_dir)], check=True, capture_output=True)
    work = tmp_path / "import"
    (work / ".polarion").mkdir(parents=True)
    (work / ".polarion" / "projects.xml").write_text('<projects><project id="acme_demo" path="/Acme/acme_demo"/></projects>')
    (work / "Acme" / "acme_demo" / ".polarion" / "pages").mkdir(parents=True)
    (work / "Acme" / "acme_demo" / ".polarion" / "pages" / "Demo Page.vm").write_text("#if($a)x#end")
    subprocess.run([_tool("svn"), "import", str(work), repo_dir.as_uri(), "-m", "test data"], check=True, capture_output=True)
    return str(repo_dir)


@needs_svn
def test_reader_lists_reads_and_reports_missing(repo):
    r = LocalSvnReader(repo, _tool("svnlook"), "http://localhost")
    assert is_repository(repo) and r.probe() == "1"
    assert sorted(r.list_dir("/")) == [".polarion/", "Acme/"]
    assert r.list_dir("Acme/acme_demo/.polarion/pages") == ["Demo Page.vm"]               # a name with a space survives
    assert "acme_demo" in r.read_text(".polarion/projects.xml")
    assert r.exists(".polarion/projects.xml") and not r.exists("nope/x.xml")
    with pytest.raises(NotFound):
        r.read_text("nope/x.xml")
    with pytest.raises(NotFound):
        r.list_dir("nope")
    assert r.url("Acme/acme_demo/.polarion/pages/Demo Page.vm") == "http://localhost/repo/Acme/acme_demo/.polarion/pages/Demo%20Page.vm"


@needs_svn
def test_reader_only_ever_runs_read_commands(repo, monkeypatch):
    seen = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: (seen.append(cmd[1]), real(cmd, *a, **k))[1])
    r = LocalSvnReader(repo, _tool("svnlook"), "http://localhost")
    r.probe(); r.list_dir("/"); r.read_text(".polarion/projects.xml"); r.exists("x")
    assert set(seen) <= {"youngest", "tree", "cat"}


@needs_svn
def test_svnlook_is_found_next_to_a_polarion_install(tmp_path):
    root = tmp_path / "polarion_root"
    (root / "bundled" / "svn" / "bin").mkdir(parents=True)
    exe = root / "bundled" / "svn" / "bin" / "svnlook.exe"
    exe.write_text("x")
    (root / "data" / "svn" / "repo").mkdir(parents=True)
    assert find_svnlook(root / "data" / "svn" / "repo") == str(exe)


@pytest.fixture
def mock_ctx(tmp_path, mock_server, monkeypatch):
    _, wsgi = mock_server("2606")
    c = build_context(db_path=tmp_path / "lr.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    monkeypatch.setenv("PAVS_SVNLOOK", _tool("svnlook") or "")
    yield c
    c.close()


@needs_svn
def test_configured_local_repository_is_used_without_any_repository_login(mock_ctx, repo):
    out = mock_ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", local_repo=repo)
    svn = out.profile.provider(ProviderKind.SVN)
    assert svn.state == ProviderState.AUTHENTICATED and "svnlook" in svn.detail and "no repository login" in svn.detail
    assert mock_ctx.environments.get(out.environment.id).local_repo_path == repo
    session = mock_ctx.connections.session(out.environment.id)
    from pavs.engines.inspection.repo import RepoConfig
    assert RepoConfig(session.svn).project_root("acme_demo") == "Acme/acme_demo/.polarion"


@needs_svn
def test_localhost_falls_back_to_the_local_repository_when_the_login_is_refused(mock_ctx, repo, monkeypatch):
    monkeypatch.setattr(connection, "find_local_repository", lambda: repo)
    out = mock_ctx.connections.quick_connect("http://localhost/polarion", "admin", "admin-test-pw",
                                             svn_user="admin", svn_password="wrong-repository-password")
    svn = out.profile.provider(ProviderKind.SVN)
    assert svn.state == ProviderState.AUTHENTICATED and "refused the login" in svn.detail
    # a server that is not on this computer never falls back to local files
    out = mock_ctx.connections.quick_connect("https://polarion.example.org/polarion", "admin", "admin-test-pw",
                                             svn_user="admin", svn_password="wrong-repository-password")
    assert out.profile.provider(ProviderKind.SVN).state == ProviderState.AUTH_FAILED


def test_a_bad_local_folder_is_explained_and_the_http_result_is_kept(mock_ctx, tmp_path):
    out = mock_ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", local_repo=str(tmp_path))
    assert any("not a Subversion repository folder" in w for w in out.profile.warnings)
    svns = [p for p in out.profile.providers if p.kind == ProviderKind.SVN]
    assert len(svns) == 1 and svns[0].state == ProviderState.AUTHENTICATED          # the normal /repo login still worked
    out = mock_ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", svn_user="admin",
                                             svn_password="wrong", local_repo=str(tmp_path))
    svns = [p for p in out.profile.providers if p.kind == ProviderKind.SVN]
    assert len(svns) == 1 and svns[0].state == ProviderState.AUTH_FAILED


@needs_svn
def test_a_server_on_a_non_default_port_never_borrows_the_local_repository(mock_ctx, repo, monkeypatch):
    """A mock, tunnel or test copy on 127.0.0.1:8810 must not silently show the real local repository's files."""
    monkeypatch.setattr(connection, "find_local_repository", lambda: repo)
    out = mock_ctx.connections.quick_connect("http://127.0.0.1:8810/polarion", "admin", "admin-test-pw",
                                             svn_user="admin", svn_password="wrong-repository-password")
    assert out.profile.provider(ProviderKind.SVN).state == ProviderState.AUTH_FAILED
    out = mock_ctx.connections.quick_connect("http://127.0.0.1:8810/polarion", "admin", "admin-test-pw",
                                             svn_user="admin", svn_password="wrong-repository-password", local_repo=repo)
    assert out.profile.provider(ProviderKind.SVN).state == ProviderState.AUTHENTICATED       # explicit choice is honoured
