"""Browser checks with Playwright (optional): does the Polarion web UI itself work for a real browser?

Opt-in. Uses the Microsoft Edge or Google Chrome that is already installed (no download); if neither can be driven,
or Playwright is not installed, every check is reported BLOCKED with the remedy: never a silent pass.

What it does (read-only, one throw-away browser profile, closed at the end):
  B01 the login page renders with the sign-in form        (no credentials needed)
  B02 the sign-in works in the browser                    (needs the username and password)
  B03 the project opens after signing in                  (navigation only, nothing is clicked or typed)
  B04 the project page raised no JavaScript errors

The only thing ever typed is the username and password into the login form. The form fields (`j_username`,
`j_password`, `form#logInForm`) were read from a real Polarion 2606 login page.
"""
from __future__ import annotations

from typing import Callable

from ...domain.enums import TestStatus
from ...domain.testing import CheckResult, Severity
from ...infra.redaction import scrub

GROUP = "Browser"
CHANNELS = ("msedge", "chrome", None)          # installed Edge, installed Chrome, Playwright's own Chromium
LOGIN_FORM = "form#logInForm"
USER_FIELD = "input[name=j_username]"
PASS_FIELD = "input[name=j_password]"
NAV_TIMEOUT_MS = 20000
SETTLE_MS = 2500


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _result(check_id: str, name: str, status: TestStatus, expected: str, actual: str, evidence: list[str] | None = None,
            severity: Severity = Severity.ERROR) -> CheckResult:
    return CheckResult(check_id=check_id, group=GROUP, name=name, status=status, severity=severity, expected=expected,
                       actual=actual, evidence=[scrub(e) or "" for e in (evidence or [])], mechanism="Playwright browser")


def _blocked_all(reason: str, remedy: str) -> list[CheckResult]:
    names = {"B01": "Login page renders", "B02": "Sign-in works in the browser",
             "B03": "Project opens in the browser", "B04": "No JavaScript errors on the project page"}
    return [_result(cid, name, TestStatus.BLOCKED, "checked in a real browser", reason, [remedy]) for cid, name in names.items()]


def run_browser_checks(base_url: str, project_id: str, username: str | None, password: str | None,
                       verify_tls: bool = True, progress: Callable[[str, int], None] | None = None) -> list[CheckResult]:
    report = progress or (lambda m, p: None)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _blocked_all("Playwright is not installed", "Install it with: pip install playwright   (it drives the Edge or Chrome you already have)")

    base = base_url.rstrip("/")
    out: list[CheckResult] = []
    js_errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser, used, errors = None, "", []
            for channel in CHANNELS:
                try:
                    browser = pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch(headless=True)
                    used = channel or "chromium"
                    break
                except PlaywrightError as exc:
                    errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0][:120]}")
            if browser is None:
                return _blocked_all("no browser could be started",
                                    "Install Microsoft Edge or Google Chrome, or run: playwright install chromium. " + " | ".join(errors))
            try:
                context = browser.new_context(ignore_https_errors=not verify_tls, viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.set_default_timeout(NAV_TIMEOUT_MS)
                page.on("pageerror", lambda e: js_errors.append(f"{page.url}: {str(e)[:200]}"))
                page.on("console", lambda m: js_errors.append(f"{page.url}: {m.text[:200]}") if m.type == "error" else None)

                # ---- B01 login page
                report("Browser: opening the login page", 10)
                try:
                    resp = page.goto(f"{base}/login", wait_until="load")
                    has_form = page.locator(LOGIN_FORM).count() == 1 and page.locator(USER_FIELD).count() == 1 \
                        and page.locator(PASS_FIELD).count() == 1
                    status = resp.status if resp else 0
                    ok = has_form and status < 400
                    out.append(_result("B01", "Login page renders", TestStatus.PASS if ok else TestStatus.FAIL,
                                       "the login page loads with a username and a password field",
                                       "login form present" if ok else f"HTTP {status}, sign-in form {'found' if has_form else 'missing'}",
                                       [f"browser: {used}", f"page title: {page.title()!r}", f"url: {page.url}"]))
                except PlaywrightError as exc:
                    out.append(_result("B01", "Login page renders", TestStatus.FAIL, "the login page loads",
                                       "the page could not be opened", [str(exc).splitlines()[0][:200], f"browser: {used}"]))
                    out += _blocked_rest("the login page did not open")
                    return out

                # ---- B02 sign-in
                if not (username and password):
                    out += [_result("B02", "Sign-in works in the browser", TestStatus.BLOCKED, "sign in through the login form",
                                    "no username and password available", ["Connect with a username and password (not only a token): "
                                                                              "the browser signs in through the login form."])]
                    out += _blocked_rest("sign-in was not possible", from_id="B03")
                    return out
                report("Browser: signing in", 40)
                page.fill(USER_FIELD, username)
                page.fill(PASS_FIELD, password)
                try:
                    with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
                        page.press(PASS_FIELD, "Enter")
                except PlaywrightError:
                    pass                                                                    # handled by the checks below
                page.wait_for_timeout(SETTLE_MS)
                signed_in = page.locator(LOGIN_FORM).count() == 0
                err_text = ""
                if not signed_in:
                    err = page.locator(".errormessage, .error, .warningmessage")
                    err_text = " ".join(err.all_inner_texts())[:200] if err.count() else ""
                out.append(_result("B02", "Sign-in works in the browser", TestStatus.PASS if signed_in else TestStatus.FAIL,
                                   "the login form accepts the account and leaves the login page",
                                   "signed in" if signed_in else "the login form is still shown",
                                   [f"url after sign-in: {page.url}"] + ([f"message: {err_text}"] if err_text else [])))
                if not signed_in:
                    out += _blocked_rest("sign-in failed", from_id="B03")
                    return out

                # ---- B03 / B04 project page (navigation only)
                report("Browser: opening the project", 70)
                js_errors.clear()
                try:
                    page.goto(f"{base}/#/project/{project_id}/", wait_until="load")
                    page.wait_for_timeout(SETTLE_MS + 1500)
                    back_to_login = page.locator(LOGIN_FORM).count() > 0
                    in_project = project_id.lower() in page.url.lower()
                    ok = not back_to_login and in_project
                    out.append(_result("B03", "Project opens in the browser", TestStatus.PASS if ok else TestStatus.FAIL,
                                       "the project's page opens for the signed-in account",
                                       "opened" if ok else ("sent back to the login page" if back_to_login else "the address left the project"),
                                       [f"url: {page.url}", f"page title: {page.title()!r}"]))
                except PlaywrightError as exc:
                    out.append(_result("B03", "Project opens in the browser", TestStatus.FAIL, "the project's page opens",
                                       "navigation failed", [str(exc).splitlines()[0][:200]]))
                errors_here = list(dict.fromkeys(js_errors))
                out.append(_result("B04", "No JavaScript errors on the project page",
                                   TestStatus.PASS if not errors_here else TestStatus.PARTIAL_PASS,
                                   "no uncaught errors or console errors while the project page loads",
                                   "none" if not errors_here else f"{len(errors_here)} error(s)", errors_here[:10], Severity.WARNING))
            finally:
                browser.close()
    except PlaywrightError as exc:
        out += [_result("B00", "Browser session", TestStatus.ERROR, "a browser session", "failed", [str(exc).splitlines()[0][:200]])]
    report("Browser: done", 100)
    return out


def _blocked_rest(reason: str, from_id: str = "B02") -> list[CheckResult]:
    names = {"B02": "Sign-in works in the browser", "B03": "Project opens in the browser", "B04": "No JavaScript errors on the project page"}
    ids = list(names)
    return [_result(cid, names[cid], TestStatus.BLOCKED, "checked in a real browser", reason) for cid in ids[ids.index(from_id):]]
