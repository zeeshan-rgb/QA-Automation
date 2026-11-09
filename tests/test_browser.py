"""Browser checks. The real-browser tests need Playwright and an installed Edge/Chrome; they are skipped otherwise."""
import sys
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from mock_polarion import PROFILES, MockPolarion
from pavs.app import build_context
from pavs.domain.enums import TestStatus as S
from pavs.engines.testing import browser
from pavs.infra.vault import MemoryBackend


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def live_mock():
    """The mock Polarion on a real localhost port (a browser cannot talk to an in-process transport)."""
    app = MockPolarion(PROFILES["2606"])
    httpd = make_server("127.0.0.1", 0, app, handler_class=_Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/polarion"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _browser_usable() -> bool:
    if not browser.playwright_available():
        return False
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            for channel in browser.CHANNELS:
                try:
                    (pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()).close()
                    return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


needs_browser = pytest.mark.skipif(not _browser_usable(), reason="Playwright with Edge/Chrome is not available")


def by_id(results):
    return {r.check_id: r for r in results}


def test_everything_is_blocked_with_a_remedy_when_playwright_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    res = browser.run_browser_checks("http://127.0.0.1:1/polarion", "acme_demo", "admin", "x")
    assert [r.check_id for r in res] == ["B01", "B02", "B03", "B04"]
    assert all(r.status == S.BLOCKED for r in res) and "pip install playwright" in " ".join(res[0].evidence)


@needs_browser
def test_full_browser_flow_against_the_mock(live_mock):
    res = by_id(browser.run_browser_checks(live_mock, "acme_demo", "admin", "admin-test-pw"))
    assert res["B01"].status == S.PASS and res["B02"].status == S.PASS and res["B03"].status == S.PASS
    assert res["B04"].status in (S.PASS, S.PARTIAL_PASS)
    assert "admin-test-pw" not in " ".join(" ".join(r.evidence) for r in res.values())


@needs_browser
def test_wrong_password_fails_sign_in_and_blocks_the_rest(live_mock):
    res = by_id(browser.run_browser_checks(live_mock, "acme_demo", "admin", "wrong-password"))
    assert res["B01"].status == S.PASS and res["B02"].status == S.FAIL
    assert res["B03"].status == S.BLOCKED and res["B04"].status == S.BLOCKED


@needs_browser
def test_no_password_blocks_sign_in_but_the_login_page_is_still_checked(live_mock):
    res = by_id(browser.run_browser_checks(live_mock, "acme_demo", None, None))
    assert res["B01"].status == S.PASS and res["B02"].status == S.BLOCKED and res["B03"].status == S.BLOCKED


@needs_browser
def test_unreachable_server_fails_the_login_check():
    res = by_id(browser.run_browser_checks("http://127.0.0.1:9/polarion", "acme_demo", "a", "b"))
    assert res["B01"].status == S.FAIL and res["B02"].status == S.BLOCKED


@needs_browser
def test_browser_checks_inside_a_run(live_mock, tmp_path):
    c = build_context(db_path=tmp_path / "b.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
    out = c.connections.quick_connect(live_mock, "admin", "admin-test-pw", token="test-token-admin")
    assert out.ok, out.message
    run = c.testing.run(out.environment.id, "acme_demo", health=False, browser=True)
    ids = {r.check_id: r for r in run.results if r.group == "Browser"}
    assert ids["B01"].status == S.PASS and ids["B02"].status == S.PASS
    assert "admin-test-pw" not in str(run.model_dump())
    c.close()


@needs_browser
def test_a_server_without_a_login_form_blocks_the_rest_and_names_an_activation_page():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Activation(BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = (404, b"<html><title>HTTP Status 404</title></html>") if self.path.endswith("/login") \
                else (200, b"<html><head><title>Polarion Activation</title></head><body>activate</body></html>")
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Activation)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        res = by_id(browser.run_browser_checks(f"http://127.0.0.1:{srv.server_port}/polarion", "p", "admin", "pw"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert res["B01"].status == S.FAIL and "activation" in res["B01"].actual.lower()
    assert res["B02"].status == S.BLOCKED and res["B03"].status == S.BLOCKED and res["B04"].status == S.BLOCKED
    assert "B00" not in res                                                           # no stray 20-second timeout error
