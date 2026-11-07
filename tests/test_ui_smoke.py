"""Offscreen UI smoke test: windows are built and filled, nothing is shown on the desktop."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from pavs.app import build_context  # noqa: E402
from pavs.infra.vault import MemoryBackend  # noqa: E402

from .conftest import ADMIN_TOKEN, make_env  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_main_window_pages(qapp, tmp_path, mock_server):
    from pavs.ui.main_window import MainWindow
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "ui.sqlite3", vault_backend=MemoryBackend(),
                        http_transport=wsgi, log_to_file=False)
    env = ctx.environments.save(make_env(name="Mock 2606"), ADMIN_TOKEN)
    ctx.connections.connect(env)
    win = MainWindow(ctx)
    assert win.nav.count() == 6
    for row in range(win.nav.count()):
        win.nav.setCurrentRow(row)
        qapp.processEvents()
    assert win.dashboard.table.rowCount() >= 1
    assert win.compatibility.live.rowCount() == len(ctx.registry.defs)
    assert win.compatibility.matrix.columnCount() == 2 + 5     # 2410, 2410 REST off, 2506, 2512, 2606
    win.environments.table.selectRow(0)
    qapp.processEvents()
    assert "Polarion 2606" in win.environments.details.toPlainText()

    # Configuration Inspector: run an inspection synchronously, then display it
    sid, snap = ctx.inspection.inspect(env.id, "acme_demo")
    win.inspector.refresh()
    win.inspector.project_box.setCurrentText("acme_demo")
    win.inspector._load_history()
    qapp.processEvents()
    assert win.inspector.history.count() >= 1
    assert win.inspector.sections.count() == len(snap.sections)
    win.inspector.sections.setCurrentRow(0)                     # Work Item types
    qapp.processEvents()
    assert win.inspector.tabs.count() >= 2
    assert win.inspector.tabs.widget(0).rowCount() == 3
    win.close()


def test_connect_and_test_page_full_flow(qapp, tmp_path, mock_server):
    """Drives the page like a user: type URL + credentials, Connect, pick the project, run, read results."""
    import time

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pavs.ui.main_window import MainWindow
    from pavs.ui.pages.connect_test import EXAMPLE
    from pavs.ui.workers import run_in_background

    _, wsgi = mock_server("2410-rest-off")
    ctx = build_context(db_path=tmp_path / "flow.sqlite3", vault_backend=MemoryBackend(),
                        http_transport=wsgi, log_to_file=False)
    win = MainWindow(ctx)
    pg = win.connect_test
    assert win.nav.currentRow() == 0 and not pg.btn_run.isEnabled()          # starts on the Connect page, run locked

    def wait(cond, secs=30):
        t = time.time()
        while not cond() and time.time() - t < secs:
            qapp.processEvents()
            QTest.qWait(10)
        assert cond()

    # callbacks must run in the UI thread (regression: they used to run in the worker thread)
    from PySide6.QtCore import QThread
    threads = []
    run_in_background(lambda p: 1, lambda r: threads.append(QThread.currentThread()),
                      on_finished=lambda: threads.append(QThread.currentThread()))
    wait(lambda: len(threads) == 2)
    assert all(t is qapp.thread() for t in threads)

    pg.url.setText("https://mock.test/polarion")
    pg.user.setText("admin")
    pg.password.setText("wrong")
    QTest.mouseClick(pg.btn_connect, Qt.LeftButton)
    wait(lambda: not pg._busy)
    assert "Sign-in failed" in pg.conn_status.text() and not pg.btn_run.isEnabled()

    pg.password.setText("admin-test-pw")
    QTest.mouseClick(pg.btn_connect, Qt.LeftButton)
    wait(lambda: not pg._busy and pg.project.count() == 2)
    assert "Connected to Polarion 2410" in pg.conn_status.text() and pg.btn_run.isEnabled()
    assert pg.password.text() == ""                                            # not kept in the field
    pg.project.setCurrentIndex(0)
    assert pg._project_id() == "acme_demo"

    a = ctx.testing.import_pasted(EXAMPLE, "Example", pg._env_id(), "acme_demo")
    pg.set_id = a.requirement_set.id
    pg._load_sets()
    pg._show_analysis(a)
    assert pg.case_table.rowCount() == len(a.cases) > 0

    QTest.mouseClick(pg.btn_run, Qt.LeftButton)
    wait(lambda: not pg._busy and pg.run_record is not None)
    assert pg.table.rowCount() == pg.run_record.total and pg.btn_export.isEnabled()
    assert pg.verdict.text() in ("PASSED", "PASSED WITH GAPS", "FAILED")
    pg.table.selectRow(0)
    qapp.processEvents()
    assert "Expected" in pg.detail.toPlainText()
    assert pg.history.rowCount() >= 1
    win.close()


def test_dashboard_totals_come_from_the_latest_runs(qapp, tmp_path, mock_server):
    from pavs.ui.pages.dashboard import DashboardPage
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "dash.sqlite3", vault_backend=MemoryBackend(),
                        http_transport=wsgi, log_to_file=False)
    env = ctx.environments.save(make_env(name="Mock 2606"), ADMIN_TOKEN)
    ctx.connections.connect(env)
    page = DashboardPage(ctx)
    assert page.c_tests.value.text() == "0" and page.c_cov.value.text() == "-"
    run = ctx.testing.run(env.id, "acme_demo", health=True)
    ctx.testing.run(env.id, "acme_demo", health=True)            # a second run of the same project must not double the totals
    page.refresh()
    assert page.c_tests.value.text() == str(run.total)
    assert page.c_pass.value.text() == str(run.counts()["pass"])
    assert page.c_cov.value.text().endswith("%")
    ctx.close()


def test_role_selector_changes_the_role(qapp, tmp_path):
    from pavs.services.roles import AppRole
    from pavs.ui.main_window import MainWindow
    ctx = build_context(db_path=tmp_path / "role.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    win = MainWindow(ctx)
    assert win.role_box.currentText() == "Admin"
    assert win.apply_role(AppRole.VIEWER) and ctx.roles.current == AppRole.VIEWER
    assert win.role_box.currentText() == "Viewer"
    ctx.close()


def test_merge_page_shows_a_comparison(qapp, tmp_path, mock_server):
    from pavs.integrations.tortoisesvn import TortoiseInfo, TortoiseLauncher
    from pavs.ui.main_window import MainWindow
    from pathlib import Path
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "mp.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False,
                        svn_launcher=TortoiseLauncher(TortoiseInfo(Path("C:/ts/TortoiseProc.exe"), None)))
    out = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", token="test-token-admin")
    assert out.ok
    win = MainWindow(ctx)
    page = win.merge
    page.refresh()
    assert page.src_env.count() == 1 and page.src_proj.count() >= 2
    page.src_proj.setCurrentText("acme_demo")
    page.dst_proj.setCurrentText("acme_test")
    rep = ctx.merge.compare(out.environment.id, "acme_demo", out.environment.id, "acme_test")
    page.report = rep
    page._show(rep)
    assert page.table.rowCount() == len([f for f in rep.files]) and page.table.rowCount() > 0
    assert "would break" in page.status.text() or "Nothing found" in page.status.text()
    assert page.btn_export.isEnabled() and page.btn_compare_ts.isEnabled()
    page.table.selectRow(0)
    assert page.btn_diff.isEnabled()
    ctx.close()


def test_second_launch_brings_the_open_window_forward(qapp, tmp_path):
    from pavs.ui import single_instance as si
    name = si.instance_name(str(tmp_path / "one"))
    assert name == si.instance_name(str(tmp_path / "ONE") + "/") and name != si.instance_name(str(tmp_path / "two"))
    assert si.forward_to_running(name, 150) is False            # nothing running: this launch becomes the main window
    raised = []
    server = si.listen(name, lambda: raised.append(1))
    assert server is not None
    import os
    import subprocess
    import sys
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = ("import sys; from PySide6.QtWidgets import QApplication; from pavs.ui import single_instance as si; "
            f"app = QApplication([]); sys.exit(0 if si.forward_to_running({name!r}) else 3)")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=src)
    child = subprocess.Popen([sys.executable, "-c", code], env=env)               # the second launch is another process
    for _ in range(600):
        qapp.processEvents()
        if child.poll() is not None:
            break
        __import__("time").sleep(0.01)
    assert child.wait(5) == 0                                   # a second launch only asks the first one to show itself
    for _ in range(50):
        qapp.processEvents()
    assert raised == [1]
    server.close()
    assert si.forward_to_running(name, 150) is False


def test_new_window_button_and_flag_start_a_separate_copy(qapp, tmp_path, monkeypatch):
    import sys
    from PySide6.QtCore import QProcess
    from pavs.ui.main_window import MainWindow
    started = []
    monkeypatch.setattr(QProcess, "startDetached", staticmethod(lambda prog, args: started.append((prog, args)) or True))
    ctx = build_context(db_path=tmp_path / "nw.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    win = MainWindow(ctx)
    win.btn_new_window.click()
    assert started == [(sys.executable, ["-m", "pavs", "--new"])]
    win.bring_to_front()                                         # must not raise on a hidden or minimised window
    from pavs.__main__ import _parser
    assert _parser().parse_known_args(["--new"])[0].new is True and _parser().parse_known_args([])[0].new is False
    ctx.close()
