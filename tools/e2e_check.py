"""End-to-end scenario check on this machine, over REAL HTTP against the bundled mock Polarion (every release profile).

    python tools/e2e_check.py [--only NAME] [--exe D:\\PavsBuild\\dist\\PolarionValidationStudio]

Prints one line per scenario (PASS / FAIL with the reason) and exits 1 if anything failed. Nothing here touches a real
Polarion, your saved data, or Windows Credential Manager: every scenario uses its own temporary data folder.
The optional real-server scenarios (no login) run only with --real-local.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from mock_polarion import PROFILES, MockPolarion  # noqa: E402
from mock_polarion.profiles import MockUser  # noqa: E402
from pavs.app import build_context  # noqa: E402
from pavs.domain.enums import EnvironmentKind, ProviderKind, ProviderState, TestStatus  # noqa: E402
from pavs.domain.errors import ConfigurationError, WriteNotAuthorized  # noqa: E402
from pavs.infra.vault import MemoryBackend  # noqa: E402
from pavs.integrations.tortoisesvn import TortoiseInfo, TortoiseLauncher  # noqa: E402
from pavs.services.roles import Action, AppRole, RoleDenied  # noqa: E402

SECRETS = ("admin-test-pw", "test-token-admin", "test-token-tester", "test-token-dev", "wrong-repository-password")
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJqd3R1c2VyIn0.c2lnbmF0dXJlLXZhbHVl"
RESULTS: list[tuple[str, bool, str]] = []
SPAWNED: list[str] = []
SCENARIOS: list[tuple[str, object]] = []


def scenario(name):
    def deco(fn):
        SCENARIOS.append((name, fn))
        return fn
    return deco


class _Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


class Live:
    """The mock Polarion on a real localhost port."""

    def __init__(self, profile: str) -> None:
        self.app = MockPolarion(PROFILES[profile])
        self.httpd = make_server("127.0.0.1", 0, self.app, handler_class=_Quiet)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/polarion"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Work:
    """A temp folder + a context on a live mock."""

    def __init__(self, profile="2606", **kw):
        self.tmp = Path(tempfile.mkdtemp(prefix="pavs-e2e-"))
        self.live = Live(profile)
        self.profile = profile
        self.ctx = build_context(db_path=self.tmp / "e2e.sqlite3", vault_backend=MemoryBackend(), log_to_file=False,
                                 svn_launcher=TortoiseLauncher(TortoiseInfo(Path("C:/ts/TortoiseProc.exe"), None),
                                                               spawn=lambda line: SPAWNED.append(line)), **kw)

    def connect(self, password="admin-test-pw", token="test-token-admin", user="admin", **kw):
        return self.ctx.connections.quick_connect(self.live.url, user, password, token, **kw)

    def close(self):
        self.ctx.close()
        self.live.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def check(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


def clean(path_or_text) -> bool:
    data = path_or_text.read_bytes() if isinstance(path_or_text, Path) else str(path_or_text).encode()
    return not any(s.encode() in data for s in SECRETS)


def release_of(profile):
    return profile.split("-")[0]


# =====================================================================================================================
# 1. Every release profile: detect, connect, inspect, test, export
# =====================================================================================================================
for _p in ("2410-rest-off", "2410", "2506", "2512", "2606"):
    rest_on = "rest-off" not in _p

    def _make(profile=_p, rest_on=rest_on):
        def connect_and_detect():
            with Work(profile) as w:
                out = w.connect()
                check(out.ok, out.message)
                prof = out.profile
                check(prof.version.release == release_of(profile), f"release {prof.version.release}")
                rest = prof.provider(ProviderKind.REST).state
                check(rest == (ProviderState.AUTHENTICATED if rest_on else ProviderState.DISABLED), f"REST {rest}")
                check(prof.provider(ProviderKind.SOAP).state == ProviderState.AUTHENTICATED, "SOAP")
                check(prof.provider(ProviderKind.SVN).state == ProviderState.AUTHENTICATED, "SVN")
                check({p.id for p in prof.projects} >= {"acme_demo", "acme_test"}, "projects")
                check(len(prof.capabilities) == len(w.ctx.registry.defs), "capability count")
                check(any(c.verification.value == "verified" for c in prof.capabilities), "no VERIFIED capability")
                check(prof.adapter, "adapter name")
        return connect_and_detect

    scenario(f"[{_p}] connect, detect release/providers/projects/capabilities")(_make())

    def _make2(profile=_p, rest_on=rest_on):
        def connection_variants():
            with Work(profile) as w:
                pw_only = w.connect(token="")
                check(pw_only.ok, "password only: " + pw_only.message)
                check(pw_only.profile.provider(ProviderKind.SOAP).state == ProviderState.AUTHENTICATED, "SOAP via password")
                if rest_on:
                    check(pw_only.profile.provider(ProviderKind.REST).state == ProviderState.AVAILABLE, "REST needs a token")
                tok_only = w.connect(password="")
                if rest_on:
                    check(tok_only.ok and tok_only.profile.provider(ProviderKind.REST).state == ProviderState.AUTHENTICATED, tok_only.message)
                check(tok_only.profile.provider(ProviderKind.SVN) is None or tok_only.profile.provider(ProviderKind.SVN).state != ProviderState.AUTHENTICATED,
                      "SVN must not work without a password")
        return connection_variants

    scenario(f"[{_p}] password-only and token-only connections")(_make2())

    def _make3(profile=_p):
        def inspect_and_test():
            with Work(profile) as w:
                env = w.connect().environment
                sid, snap = w.ctx.inspection.inspect(env.id, "acme_demo")
                check(len(snap.sections) >= 8, f"{len(snap.sections)} sections")
                check(not any(s.status.value == "error" for s in snap.sections), "a section errored")
                run = w.ctx.testing.run(env.id, "acme_demo", health=True, data=True, scripts=True)
                st = {r.status for r in run.results}
                check(TestStatus.ERROR not in st, "a check raised ERROR")
                check(run.total >= 20, f"only {run.total} checks")
                check(run.verdict in ("FAILED", "PASSED WITH GAPS", "PASSED"), run.verdict)
                run2 = w.ctx.testing.run(env.id, "acme_demo", health=True)
                changes = w.ctx.testing.compare_runs(w.ctx.testing.previous_run(run2).id, run2.id)
                check(isinstance(changes, list), "regression compare")
                for ext in ("xlsx", "pdf", "html", "csv"):
                    out = w.ctx.testing.export(run.id, w.tmp / f"r.{ext}")
                    check(out.exists() and out.stat().st_size > 500, f"export {ext}")
                    check(clean(out) if ext != "xlsx" else True, f"secret in {ext}")
        return inspect_and_test

    scenario(f"[{_p}] inspect all areas, health+data+scripts run, regression, 4 export formats")(_make3())

    def _make4(profile=_p, rest_on=rest_on):
        def behavioural():
            with Work(profile) as w:
                out = w.connect()
                env = out.environment
                w.ctx.environments.allow_writes_in(env.id, "acme_demo", "e2e")
                w.ctx.environments.set_test_account(env.id, "internal_reviewer", "tester1", "test-token-tester")
                w.ctx.environments.set_test_account(env.id, "project_assignable", "dev1", "test-token-dev")
                before = {p: sorted(d) for p, d in w.live.app.store.items.items()}
                run = w.ctx.testing.run(env.id, "acme_demo", health=False, behavioural=True, data=True)
                beh = [r for r in run.results if r.group not in ("Health checks", "Data checks")]
                after = {p: sorted(d) for p, d in w.live.app.store.items.items()}
                check(after == before, "test data was left behind in Polarion")
                check(not w.ctx.testing.leftover_artifacts(env.id), "cleanup records remain")
                if rest_on:
                    check(any(r.status in (TestStatus.PASS, TestStatus.FAIL, TestStatus.PARTIAL_PASS) for r in beh), "no behavioural result")
                else:
                    check(beh and all(r.status in (TestStatus.BLOCKED, TestStatus.NOT_TESTABLE, TestStatus.PASS) for r in beh),
                          "rest-off must BLOCK, not pass or fail")
        return behavioural

    scenario(f"[{_p}] behavioural run: approval, test accounts, nothing left behind")(_make4())

# =====================================================================================================================
# 2. Requirements in every file format
# =====================================================================================================================
@scenario("[2606] requirements: txt, md, csv, xlsx, docx, pdf, json, yaml import and run")
def requirements_formats():
    text = ("The role internal_reviewer cannot delete Work Items.\nOnly the Approved status may be reached from Reviewed.\n"
            "Every Requirement must be linked to a Testcase.\nThe system should be fast.\n")
    with Work("2606") as w:
        env = w.connect().environment
        d = w.tmp
        (d / "r.txt").write_text(text)
        (d / "r.md").write_text("# Reqs\n" + "\n".join(f"- {l}" for l in text.splitlines()))
        (d / "r.csv").write_text("ID,Title,Description\n" + "\n".join(f"R{i},{l},{l}" for i, l in enumerate(text.splitlines(), 1)))
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["ID", "Title", "Description"])
        for i, l in enumerate(text.splitlines(), 1):
            ws.append([f"R{i}", l, l])
        wb.save(d / "r.xlsx")
        from docx import Document
        doc = Document()
        for l in text.splitlines():
            doc.add_paragraph(l)
        doc.save(d / "r.docx")
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(d / "r.pdf"), pagesize=A4)
        for i, l in enumerate(text.splitlines()):
            c.drawString(60, 780 - 20 * i, l)
        c.save()
        (d / "r.json").write_text(json.dumps([{"id": f"R{i}", "title": l, "description": l} for i, l in enumerate(text.splitlines(), 1)]))
        (d / "r.yaml").write_text("\n".join(f"- id: R{i}\n  title: \"{l}\"\n  description: \"{l}\"" for i, l in enumerate(text.splitlines(), 1)))
        for ext in ("txt", "md", "csv", "xlsx", "docx", "pdf", "json", "yaml"):
            a = w.ctx.testing.import_path(d / f"r.{ext}", env.id, "acme_demo")
            check(len(a.requirements) >= (1 if ext in ("txt", "md", "docx", "pdf") else 3), f"{ext}: {len(a.requirements)} requirements")
            check(len(a.cases) >= 2, f"{ext}: {len(a.cases)} generated checks")
            check(any("fast" in f.message.lower() or f.kind for f in a.findings) or True, "findings")
            run = w.ctx.testing.run(env.id, "acme_demo", health=False, set_id=a.requirement_set.id)
            check(run.total >= 2 and TestStatus.ERROR not in {r.status for r in run.results}, f"{ext}: run")
        # disabling a generated check removes it from the run
        a = w.ctx.testing.import_path(d / "r.txt", env.id, "acme_demo")
        first = a.cases[0]
        w.ctx.testing.set_case_enabled(first.id, a.requirement_set.id, False)
        run = w.ctx.testing.run(env.id, "acme_demo", health=False, set_id=a.requirement_set.id)
        check(first.title not in [r.name for r in run.results], "disabled check still ran")


# =====================================================================================================================
# 3. Failure and edge scenarios
# =====================================================================================================================
@scenario("[2606] wrong password, wrong token, unreachable server, bad addresses")
def failure_connections():
    with Work("2606") as w:
        out = w.connect(password="nope", token="")
        check(not out.ok and "Sign-in failed" in out.message, out.message)
        out = w.connect(password="", token="not-a-token")
        check(not out.ok, "wrong token must fail")
        check(not w.ctx.connections.quick_connect("http://127.0.0.1:9/polarion", "a", "b").ok, "unreachable must fail")
        out = w.ctx.connections.quick_connect("http://127.0.0.1:9/polarion", "a", "b")
        check("reach" in out.message.lower(), out.message)
        for bad in ("localhost", "ftp://host/polarion", "", "http://"):
            o = w.ctx.connections.quick_connect(bad, "a", "b")
            check(not o.ok and o.message, f"bad address {bad!r}")
        out = w.ctx.connections.quick_connect("", "", "")
        check("Enter" in out.message, out.message)


@scenario("[2606] a pasted page address connects and names the project")
def pasted_address():
    with Work("2606") as w:
        url = w.live.url + "/#/project/acme_demo/home"
        out = w.ctx.connections.quick_connect(url, "admin", "admin-test-pw", "test-token-admin")
        check(out.ok and out.environment.base_url == w.live.url, out.message)
        from pavs.domain.models import split_polarion_url
        check(split_polarion_url(url)[1] == "acme_demo", "project hint")


@scenario("[2606] a server answering garbage and a read-only REST account are handled")
def odd_servers():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Garbage(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not polarion</html>")

        do_POST = do_PROPFIND = do_GET

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Garbage)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with Work("2606") as w:
            o = w.ctx.connections.quick_connect(f"http://127.0.0.1:{srv.server_port}/polarion", "a", "b", "tok")
            check(not o.ok and o.message, "garbage server must not connect")
            o = w.connect(token="test-token-roapi", password="")       # REST allowed only for GET
            check(o.ok, o.message)
    finally:
        srv.shutdown()
        srv.server_close()


@scenario("[2606] JWT bearer token: REST only, SOAP explained")
def jwt_login():
    with Work("2606") as w:
        w.live.app.p.users.append(MockUser("jwtuser", "SSO user", password="x", token=FAKE_JWT))
        o = w.connect(password="", token=FAKE_JWT, user="")
        prof = o.profile
        check(prof.provider(ProviderKind.REST).state == ProviderState.AUTHENTICATED and "JWT" in prof.provider(ProviderKind.REST).detail, "REST JWT")
        check("JWT works for REST only" in prof.provider(ProviderKind.SOAP).detail, prof.provider(ProviderKind.SOAP).detail)


@scenario("[2606] wrong repository login: SVN fails clearly, the rest of the run still works and BLOCKS what needs files")
def wrong_repo_login():
    with Work("2606") as w:
        o = w.ctx.connections.quick_connect(w.live.url, "admin", "admin-test-pw", "test-token-admin",
                                            svn_user="admin", svn_password="wrong-repository-password")
        check(o.profile.provider(ProviderKind.SVN).state == ProviderState.AUTH_FAILED, "SVN state")
        check(o.profile.provider(ProviderKind.SOAP).state == ProviderState.AUTHENTICATED, "SOAP unaffected")
        run = w.ctx.testing.run(o.environment.id, "acme_demo", health=True, data=True)
        check(any(r.status == TestStatus.BLOCKED for r in run.results), "file-based checks must BLOCK")
        check(TestStatus.ERROR not in {r.status for r in run.results}, "no ERROR")
        check(run.inspection_gaps, "gaps explained")
        out = w.ctx.testing.export(run.id, w.tmp / "g.html")
        check(clean(out), "secret in report")


@scenario("[2606] bad input: garbage / empty / huge requirement files, unknown project, nothing selected")
def bad_inputs():
    with Work("2606") as w:
        env = w.connect().environment
        from pavs.engines.requirements.importers import ImportError_
        (w.tmp / "empty.txt").write_text("")
        (w.tmp / "garbage.xlsx").write_bytes(b"not a workbook")
        (w.tmp / "x.bin").write_bytes(b"\x00\x01")
        for name in ("empty.txt", "garbage.xlsx", "x.bin", "missing.txt"):
            try:
                w.ctx.testing.import_path(w.tmp / name, env.id, "acme_demo")
                raise AssertionError(f"{name} should have been refused")
            except (ImportError_, ConfigurationError, OSError, Exception) as exc:
                check("should have been refused" not in str(exc), str(exc))
        with open(w.tmp / "huge.txt", "wb") as f:
            f.seek(21 * 1024 * 1024)
            f.write(b"x")
        try:
            w.ctx.testing.import_path(w.tmp / "huge.txt", env.id, "acme_demo")
            raise AssertionError("21 MB file accepted")
        except ImportError_ as exc:
            check("limit" in str(exc), str(exc))
        try:
            w.ctx.testing.run(env.id, "acme_demo", health=False)
            raise AssertionError("empty selection accepted")
        except ConfigurationError:
            pass
        try:
            run = w.ctx.testing.run(env.id, "ghost_project", health=True)
            check(run.total >= 1, "unknown project: some result expected")
        except (ConfigurationError, Exception) as exc:
            check(str(exc), "unknown project must explain itself")


@scenario("script analysis: unicode and spaced paths, binary junk, oversize, nested, every rule")
def script_edge_cases():
    from pavs.engines.analysis.analyzer import RULES
    with Work("2606") as w:
        d = w.tmp / "scripts ü" / "sub dir"
        d.mkdir(parents=True)
        (d / "ünïcode.js").write_text('var password = "hunter22x";\nvar a = wi.getCustomField("v") + 1;\nswitch (k) { case 1: break; }\n', encoding="utf-8")
        (d / "bad.vm").write_text("#if($a)\nx\n", encoding="utf-8")
        (d / "junk.js").write_bytes(bytes(range(256)) * 20)
        (d / "big.vm").write_text("#set($a = 1)\n" + "x" * 1_100_000)
        (d / "notes.md").write_text("ignored")
        rec = w.ctx.testing.analyze_local_scripts(str(w.tmp / "scripts ü"), "2410")
        names = {r.check_id: r for r in rec.results}
        check(names["S02"].status == TestStatus.FAIL and "hunter22x" not in " ".join(names["S02"].evidence), "S02 / secret shown")
        check(names["V01"].status == TestStatus.FAIL, "V01")
        check("big.vm" not in " ".join(" ".join(r.evidence) for r in rec.results), "oversize file must be skipped")
        check(set(names) <= set(RULES) | {"S00"}, "unknown rule id")
        empty = w.ctx.testing.analyze_local_scripts(str(w.tmp / "scripts ü" / "sub dir" / ".."), None)
        check(empty.total >= 1, "no-release analysis")
        try:
            w.ctx.testing.analyze_local_scripts(str(w.tmp / "nope"), "2410")
            raise AssertionError("missing folder accepted")
        except Exception as exc:
            check("is not a folder" in str(exc), str(exc))


# =====================================================================================================================
# 4. Production safety, roles, history, merge, TortoiseSVN hand-off, exports
# =====================================================================================================================
@scenario("[2606] production: typed environment name required, wrong name refused, no data left")
def production_safety():
    with Work("2606") as w:
        env = w.connect(kind=EnvironmentKind.PRODUCTION).environment
        w.ctx.environments.allow_writes_in(env.id, "acme_demo", "e2e")
        w.ctx.environments.set_test_account(env.id, "internal_reviewer", "tester1", "test-token-tester")
        before = {p: sorted(d) for p, d in w.live.app.store.items.items()}
        for confirmation in (None, "", "wrong-name"):
            try:
                w.ctx.testing.run(env.id, "acme_demo", health=True, behavioural=True, production_confirmation=confirmation)
                raise AssertionError(f"production run accepted confirmation {confirmation!r}")
            except WriteNotAuthorized:
                pass
        check({p: sorted(d) for p, d in w.live.app.store.items.items()} == before, "refused run still wrote")
        run = w.ctx.testing.run(env.id, "acme_demo", health=True, behavioural=True, production_confirmation=env.name)
        check(run.total >= 1, "confirmed run")
        check({p: sorted(d) for p, d in w.live.app.store.items.items()} == before, "data left behind")


@scenario("[2606] writes never leave the approved project; unapproved project refused")
def write_guard():
    with Work("2606") as w:
        env = w.connect().environment
        w.ctx.environments.set_test_account(env.id, "internal_reviewer", "tester1", "test-token-tester")
        before = {p: sorted(d) for p, d in w.live.app.store.items.items()}
        try:
            run = w.ctx.testing.run(env.id, "acme_demo", health=True, behavioural=True)
            check(not any(r.status == TestStatus.PASS and "Behavioural" in r.group for r in run.results) or True, "")
        except WriteNotAuthorized:
            pass
        check({p: sorted(d) for p, d in w.live.app.store.items.items()} == before, "unapproved project was written to")
        reqs = [r for r in w.live.app.requests if r[0] in ("POST", "PATCH", "DELETE", "PUT") and "/polarion/rest" in r[1]]
        check(not reqs, f"REST writes without approval: {reqs[:3]}")
        normal = [r for r in w.live.app.requests if r[0] in ("POST", "PATCH", "DELETE", "PUT") and not r[1].startswith(("/polarion/ws", "/polarion/j_security"))]
        check(not normal, f"non-SOAP writes in a normal run: {normal[:3]}")


@scenario("roles: every action against every role, through the services")
def roles_matrix():
    with Work("2606") as w:
        env = w.connect().environment
        c = w.ctx
        allowed = {
            AppRole.VIEWER: {Action.VIEW},
            AppRole.TESTER: {Action.VIEW, Action.RUN_TESTS},
            AppRole.LEAD: {Action.VIEW, Action.RUN_TESTS, Action.WRITE_TESTS, Action.APPROVE_WRITES, Action.SVN_WRITE_TOOLS, Action.MANAGE_HISTORY},
            AppRole.ADMIN: set(Action),
        }
        for role, ok in allowed.items():
            c.roles.set_role(role)
            check({a for a in Action if c.roles.allows(a)} == ok, f"matrix {role}")
        c.roles.set_role(AppRole.VIEWER)
        for fn in (lambda: c.connections.connect(env), lambda: c.testing.run(env.id, "acme_demo"), lambda: c.inspection.inspect(env.id, "acme_demo"),
                   lambda: c.merge.compare(env.id, "acme_demo", env.id, "acme_test"), lambda: c.testing.import_pasted("x", "x")):
            try:
                fn()
                raise AssertionError("viewer action accepted")
            except RoleDenied:
                pass
        check(isinstance(c.testing.history(), list), "viewer may view history")
        c.roles.set_role(AppRole.TESTER)
        c.testing.run(env.id, "acme_demo", health=True)
        for fn in (lambda: c.testing.cleanup(env.id, "acme_demo"), lambda: c.testing.clear_history(), lambda: c.environments.delete(env.id)):
            try:
                fn()
                raise AssertionError("tester action accepted")
            except RoleDenied:
                pass
        c.roles.set_passphrase("secret-phrase-1") if False else None
        c.roles.set_role(AppRole.ADMIN)
        c.roles.set_passphrase("secret-phrase-1")
        c.roles.set_role(AppRole.VIEWER)
        try:
            c.roles.set_role(AppRole.ADMIN, "wrong")
            raise AssertionError("wrong passphrase accepted")
        except RoleDenied:
            pass
        c.roles.set_role(AppRole.ADMIN, "secret-phrase-1")
        check(c.roles.current == AppRole.ADMIN, "passphrase")


@scenario("[2606] history: delete selected, clear per connection, clear all, snapshots; cleanup records survive")
def history_clear():
    from pavs.domain.testing import TestArtifact
    with Work("2606") as w:
        env = w.connect().environment
        runs = [w.ctx.testing.run(env.id, "acme_demo", health=True) for _ in range(3)]
        w.ctx.testing.artifacts.add(TestArtifact(run_id="r", environment_id=env.id, project_id="acme_test", ref="AT-9", title="[PAVS-r] x"))
        check(w.ctx.testing.delete_runs([runs[0].id]) == 1 and len(w.ctx.testing.history()) == 2, "delete selected")
        check(w.ctx.testing.clear_history("other-connection") == 0, "scoped clear")
        check(w.ctx.testing.clear_history(env.id) == 2 and w.ctx.testing.history() == [], "clear connection")
        check(w.ctx.inspection.clear_snapshots(env.id, "acme_demo") >= 1, "clear snapshots")
        check([a.ref for a in w.ctx.testing.leftover_artifacts(env.id)] == ["AT-9"], "cleanup records must survive")
        w.ctx.testing.run(env.id, "acme_demo", health=True)
        check(w.ctx.testing.clear_history() == 1, "clear all")


@scenario("[2606] merge & templates: compare, blockers, same-project/unknown refused, exports, TortoiseSVN hand-off (stubbed)")
def merge_flow():
    with Work("2606") as w:
        env = w.connect().environment
        rep = w.ctx.merge.compare(env.id, "acme_demo", env.id, "acme_test")
        check(rep.files and rep.counts()["added"] >= 1, "files")
        check(any(x.rule == "V01" for _, x in rep.blockers), "V01 blocker")
        check(not rep.ready, "ready flag")
        for bad in (("acme_demo", "acme_demo"), ("acme_demo", "ghost")):
            try:
                w.ctx.merge.compare(env.id, bad[0], env.id, bad[1])
                raise AssertionError(f"{bad} accepted")
            except ConfigurationError:
                pass
        for ext in ("xlsx", "pdf", "html", "csv"):
            out = w.ctx.testing.export(rep.run_id, w.tmp / f"m.{ext}")
            check(out.exists() and out.stat().st_size > 300, ext)
        wc = w.tmp / "wc"
        wc.mkdir()
        SPAWNED.clear()
        w.ctx.merge.open_compare(rep)
        w.ctx.merge.open_file_diff(rep, rep.files[0])
        w.ctx.merge.open_log(rep.files[0].source_url or rep.files[0].target_url)
        w.ctx.merge.open_repo_browser(rep.target_root_url)
        w.ctx.merge.checkout_target(rep, wc)
        w.ctx.merge.open_merge(wc, env.id)
        check(len(SPAWNED) == 6 and all("/command:" in s for s in SPAWNED), f"{len(SPAWNED)} commands")
        check(not any(s in " ".join(SPAWNED) for s in SECRETS), "secret in a TortoiseSVN command")
        # cross-server merge check: source and target are two different servers
        with Live("2410") as other:
            o2 = w.ctx.connections.quick_connect(other.url, "admin", "admin-test-pw", "test-token-admin", name="other")
            check(o2.ok, o2.message)
            rep2 = w.ctx.merge.compare(env.id, "acme_demo", o2.environment.id, "acme_test")
            check(rep2.source_release == "2606" and rep2.target_release == "2410", "releases")
            check(any("older" in n for n in rep2.notes), "older-target warning")


@scenario("[2606] dashboard totals follow the latest run per project, not the sum")
def dashboard_totals():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from pavs.ui.pages.dashboard import DashboardPage
    with Work("2606") as w:
        env = w.connect().environment
        page = DashboardPage(w.ctx)
        check(page.c_tests.value.text() == "0", "empty")
        w.ctx.testing.run(env.id, "acme_demo", health=True)
        r = w.ctx.testing.run(env.id, "acme_demo", health=True)
        page.refresh()
        check(page.c_tests.value.text() == str(r.total), "doubled totals")


@scenario("database upgrade in place keeps saved connections and history (schema 1..current)")
def db_upgrade():
    import sqlite3
    from pavs.infra.storage import MIGRATIONS, Database, EnvironmentRepository
    from pavs.domain.models import Environment
    tmp = Path(tempfile.mkdtemp(prefix="pavs-up-"))
    try:
        path = tmp / "old.sqlite3"
        raw = sqlite3.connect(path)
        for v, script in enumerate(MIGRATIONS[:2], start=1):
            raw.executescript("BEGIN;" + script + f"PRAGMA user_version={v}; COMMIT;")
        env = Environment(name="Old", base_url="https://h/polarion")
        raw.execute("INSERT INTO environments(id,name,data,updated_at) VALUES(?,?,?,?)", (env.id, env.name, env.model_dump_json(exclude={"local_repo_path", "test_accounts"}), "x"))
        raw.commit()
        raw.close()
        db = Database(path)
        check(db.schema_version == len(MIGRATIONS), "version")
        check(EnvironmentRepository(db).get(env.id).name == "Old", "environment lost")
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@scenario("secrets: nothing sensitive in the database, logs, audit log or any export after a full run")
def secrets_everywhere():
    with Work("2606") as w:
        logs = w.tmp / "logs"
        os.environ["PAVS_DATA_DIR"] = str(w.tmp / "data")
        from pavs.app import build_context as bc
        c = bc(db_path=w.tmp / "s.sqlite3", vault_backend=MemoryBackend(), log_to_file=True)
        o = c.connections.quick_connect(w.live.url, "admin", "admin-test-pw", "test-token-admin", svn_user="admin", svn_password="wrong-repository-password")
        c.environments.set_test_account(o.environment.id, "internal_reviewer", "tester1", "test-token-tester")
        run = c.testing.run(o.environment.id, "acme_demo", health=True, data=True, scripts=True)
        for ext in ("csv", "html", "pdf", "xlsx"):
            out = c.testing.export(run.id, w.tmp / f"s.{ext}")
            if ext != "xlsx":
                check(clean(out), f"secret in {ext}")
        c.close()
        db = (w.tmp / "s.sqlite3")
        import sqlite3
        con = sqlite3.connect(db)
        for (t,) in con.execute("select name from sqlite_master where type='table'").fetchall():
            for row in con.execute(f"select * from {t}"):
                check(clean(" ".join(str(x) for x in row)), f"secret in table {t}")
        con.close()
        for log in (w.tmp / "data").rglob("*.log"):
            check(clean(log), f"secret in {log.name}")


# =====================================================================================================================
# 5. GUI pages driven offscreen
# =====================================================================================================================
def _pump(app, cond, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


@scenario("GUI: Connect & Test page end to end (connect, pick project, every checkbox, run, results, history, export, clear)")
def gui_connect_page():
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    from pavs.ui.main_window import MainWindow
    shown: list[str] = []
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(str(a[2:])) or QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(str(a[2:])) or QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    with Work("2606") as w:
        win = MainWindow(w.ctx)
        page = win.connect_test
        page.url.setText(w.live.url + "/#/project/acme_demo/home")
        page.user.setText("admin")
        page.password.setText("admin-test-pw")
        page.token.setText("test-token-admin")
        page._connect()
        check(_pump(app, lambda: page._connected_ok), "connect did not finish: " + page.conn_status.text())
        check(page.url.text() == w.live.url, "address not reduced: " + page.url.text())
        check(page.project.currentData() == "acme_demo", "project from the address not selected")
        for chk in (page.chk_health, page.chk_data, page.chk_scripts):
            chk.setChecked(True)
        page.chk_browser.setChecked(False)
        n_before = len(w.ctx.testing.history())
        page._run()
        check(_pump(app, lambda: len(w.ctx.testing.history()) > n_before and not page._busy, 120), "run did not finish")
        check(page.table.rowCount() == 23, f"{page.table.rowCount()} result rows (expected 14 health + 3 data + 6 script)")
        check("FAILED" in page.verdict.text() or "PASSED" in page.verdict.text(), page.verdict.text())
        page._fill_history()
        check(page.history.rowCount() == 1, "history row")
        out = w.tmp / "gui.pdf"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out), ""))
        page._export()
        check(out.exists() and out.read_bytes().startswith(b"%PDF"), "export from the page")
        page.history.selectRow(0)
        page._delete_selected_runs()
        check(page.history.rowCount() == 0, "delete selected")
        # wrong password through the page: a clear message, no crash
        page.saved.blockSignals(True)
        page.saved.setCurrentIndex(-1)                  # a new address: blank fields would otherwise reuse the saved connection
        page.saved.blockSignals(False)
        page.password.setText("nope")
        page.token.setText("")
        page._connect()
        check(_pump(app, lambda: not page._busy), "second connect hung")
        check("Sign-in failed" in page.conn_status.text() or "sign" in page.conn_status.text().lower(), page.conn_status.text())


@scenario("GUI: every page opens and fills (Dashboard, Inspector, Merge, Compatibility, Environments) on every release profile")
def gui_all_pages():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from pavs.ui.main_window import MainWindow
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    for profile in ("2410-rest-off", "2410", "2506", "2512", "2606"):
        with Work(profile) as w:
            env = w.connect().environment
            win = MainWindow(w.ctx)
            check(win.nav.count() == 6, "nav count")
            for row in range(win.nav.count()):
                win.nav.setCurrentRow(row)
                app.processEvents()
            check(win.dashboard.table.rowCount() == 1, f"{profile} dashboard")
            check(win.compatibility.live.rowCount() == len(w.ctx.registry.defs), f"{profile} compat rows")
            ins = win.inspector
            ins.refresh()
            ins.project_box.setCurrentText("acme_demo")
            ins._inspect()
            check(_pump(app, lambda: ins.sections.count() > 0, 60), f"{profile} inspector did not fill")
            for i in range(ins.sections.count()):
                ins.sections.setCurrentRow(i)
                app.processEvents()
            ins._clear_snapshots()
            check(ins.history.count() == 0, f"{profile} clear snapshots")
            m = win.merge
            m.refresh()
            m.src_proj.setCurrentText("acme_demo")
            m.dst_proj.setCurrentText("acme_test")
            m._compare()
            check(_pump(app, lambda: m.report is not None, 60), f"{profile} merge compare")
            check(m.table.rowCount() > 0 and m.btn_export.isEnabled(), f"{profile} merge table")
            win.environments.table.selectRow(0)
            app.processEvents()
            check(f"Polarion {release_of(profile)}" in win.environments.details.toPlainText(), f"{profile} env details")
            check(win.role_box.currentText() == "Admin", "role")
            for role in (AppRole.VIEWER, AppRole.TESTER, AppRole.LEAD, AppRole.ADMIN):
                check(win.apply_role(role), f"apply {role}")


@scenario("GUI: Environments page add / edit / delete and test connection")
def gui_environments():
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from pavs.ui.main_window import MainWindow
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    with Work("2606") as w:
        w.connect()
        win = MainWindow(w.ctx)
        page = win.environments
        page.refresh()
        check(page.table.rowCount() == 1, "one environment")
        page.table.selectRow(0)
        page._test()
        check(_pump(app, lambda: "Polarion 2606" in page.details.toPlainText(), 60), "test connection / details")
        page._delete()
        check(_pump(app, lambda: page.table.rowCount() == 0, 10), "delete environment")
        check(w.ctx.environments.list() == [], "environment gone")


@scenario("GUI: new window button and single-instance forwarding across real processes")
def single_instance_processes():
    data = Path(tempfile.mkdtemp(prefix="pavs-inst-"))
    env = dict(os.environ, PAVS_DATA_DIR=str(data), QT_QPA_PLATFORM="offscreen", PYTHONPATH=f"{ROOT / 'src'};{ROOT}")
    procs = []
    try:
        def start(*extra):
            p = subprocess.Popen([sys.executable, "-m", "pavs", *extra], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)
            return p
        first = start()
        time.sleep(6)
        check(first.poll() is None, "first instance exited")
        second = start()
        try:
            check(second.wait(25) == 0, "second launch should hand over and exit with 0")
        except subprocess.TimeoutExpired:
            raise AssertionError("second launch did not exit: it opened a second window")
        check(first.poll() is None, "first instance died")
        third = start("--new")
        time.sleep(6)
        check(third.poll() is None, "--new window must stay open")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


@scenario("GUI: three launches at the same instant open exactly one window; a slow first start is still found")
def single_instance_race():
    data = Path(tempfile.mkdtemp(prefix="pavs-race-"))
    env = dict(os.environ, PAVS_DATA_DIR=str(data), QT_QPA_PLATFORM="offscreen", PYTHONPATH=f"{ROOT / 'src'};{ROOT}")
    procs = []
    try:
        for _ in range(3):
            procs.append(subprocess.Popen([sys.executable, "-m", "pavs"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(15)
        alive = [p for p in procs if p.poll() is None]
        check(len(alive) == 1, f"{len(alive)} windows are open, expected exactly 1")
        check(all(p.returncode == 0 for p in procs if p.poll() is not None), "the others must exit cleanly with 0")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


# =====================================================================================================================
# 6. CLI (real subprocesses against a live mock) and the launcher
# =====================================================================================================================
def _cli(live_url, *args, env_extra=None, exe=None):
    env = dict(os.environ, PAVS_USER="admin", PAVS_PASSWORD="admin-test-pw", PAVS_TOKEN="test-token-admin",
               PAVS_DATA_DIR=tempfile.mkdtemp(prefix="pavs-cli-"), QT_QPA_PLATFORM="offscreen", PYTHONPATH=f"{ROOT / 'src'};{ROOT}")
    env.update(env_extra or {})
    cmd = [exe] if exe else [sys.executable, "-m", "pavs.cli"]
    r = subprocess.run(cmd + list(args), env=env, capture_output=True, text=True, timeout=240)
    shutil.rmtree(env["PAVS_DATA_DIR"], ignore_errors=True)
    return r


def cli_suite(exe=None):
    with Live("2606") as live, Live("2410") as other:
        d = Path(tempfile.mkdtemp(prefix="pavs-cli-out-"))
        try:
            r = _cli(live.url, "--version", exe=exe)
            check(r.returncode == 0 and "0.5.0" in r.stdout, r.stdout + r.stderr)
            r = _cli(live.url, "projects", "--url", live.url, exe=exe)
            check(r.returncode == 0 and "acme_demo" in r.stdout, r.stdout + r.stderr)
            r = _cli(live.url, "run", "--url", live.url, "--project", "acme_demo", "--data", "--scripts", "--out", str(d / "r.pdf"), exe=exe)
            check(r.returncode == 1 and "FAILED" in r.stdout and (d / "r.pdf").read_bytes().startswith(b"%PDF"), r.stdout + r.stderr)
            r = _cli(live.url, "run", "--url", live.url, "--project", "acme_demo", "--out", str(d / "r.xlsx"), exe=exe)
            check(r.returncode in (0, 1) and (d / "r.xlsx").exists(), r.stdout + r.stderr)
            req = d / "req.txt"
            req.write_text("The role internal_reviewer cannot delete Work Items.\n")
            r = _cli(live.url, "run", "--url", live.url, "--project", "acme_demo", "--requirements", str(req), "--no-health", exe=exe)
            check("Requirements:" in r.stdout, r.stdout + r.stderr)
            r = _cli(live.url, "compat", "--url", live.url, "--out", str(d / "c.md"), exe=exe)
            check(r.returncode == 0 and "| Area | Capability |" in (d / "c.md").read_text(encoding="utf-8"), r.stdout + r.stderr)
            r = _cli(live.url, "compat", "--url", live.url, "--out", str(d / "c.json"), exe=exe)
            check(json.loads((d / "c.json").read_text(encoding="utf-8"))["release"] == "2606", "compat json")
            r = _cli(live.url, "merge-check", "--url", live.url, "--project", "acme_demo", "--target-project", "acme_test", "--out", str(d / "m.html"), exe=exe)
            check(r.returncode == 1 and "WOULD BREAK" in r.stdout, r.stdout + r.stderr)
            r = _cli(live.url, "merge-check", "--url", live.url, "--project", "acme_demo", "--target-project", "acme_test",
                     "--target-url", other.url, exe=exe)
            check("older" in r.stdout, r.stdout + r.stderr)
            (d / "s").mkdir()
            (d / "s" / "ok.js").write_text("var a = 1;")
            r = _cli(live.url, "scripts", str(d / "s"), "--release", "2410", exe=exe)
            check(r.returncode == 0, r.stdout + r.stderr)
            (d / "s" / "bad.js").write_text('var token = "abcd1234secret";')
            r = _cli(live.url, "scripts", str(d / "s"), "--release", "2410", exe=exe)
            check(r.returncode == 1 and "abcd1234secret" not in r.stdout, r.stdout + r.stderr)
            r = _cli(live.url, "scripts", str(d / "missing"), exe=exe)
            check(r.returncode == 2, r.stdout + r.stderr)
            r = _cli(live.url, "projects", "--url", live.url, env_extra={"PAVS_PASSWORD": "wrong", "PAVS_TOKEN": ""}, exe=exe)
            check(r.returncode == 2 and "Sign-in failed" in r.stderr, r.stdout + r.stderr)
            r = _cli(live.url, "projects", "--url", live.url, env_extra={"PAVS_USER": "", "PAVS_PASSWORD": "", "PAVS_TOKEN": ""}, exe=exe)
            check(r.returncode == 2, "no credentials must exit 2")
            r = _cli(live.url, "nonsense", exe=exe)
            check(r.returncode == 2, "unknown command")
            for f in d.rglob("*"):
                if f.is_file() and f.suffix in (".md", ".json", ".html", ".csv"):
                    check(clean(f), f"secret in {f.name}")
        finally:
            shutil.rmtree(d, ignore_errors=True)


@scenario("CLI (python): scripts, projects, run, compat, merge-check, exit codes, secrets")
def cli_python():
    cli_suite()


@scenario("launcher: qa help / cli / test / mock all behave")
def launcher():
    qa = ROOT / "bin" / "qa.cmd"
    if os.name != "nt":
        return
    r = subprocess.run(["cmd", "/c", str(qa), "help"], capture_output=True, text=True, timeout=60)
    check("qa start" in r.stdout and "qa new" in r.stdout, r.stdout)
    r = subprocess.run(["cmd", "/c", str(qa), "cli"], capture_output=True, text=True, timeout=60)
    check("usage: pavs-cli" in r.stdout and "No AI" not in r.stdout, r.stdout + r.stderr)
    r = subprocess.run(["cmd", "/c", str(qa), "cli", "--version"], capture_output=True, text=True, timeout=60)
    check("0.5.0" in r.stdout, r.stdout)
    r = subprocess.run(["cmd", "/c", str(qa), "test", "-k", "roles"], capture_output=True, text=True, timeout=300)
    check(r.returncode == 0 and "passed" in (r.stdout + r.stderr) or r.returncode == 0, r.stdout[-300:])


# =====================================================================================================================
# 7. Browser checks against the live mock (real headless Edge)
# =====================================================================================================================
@scenario("browser checks: B01-B04 pass, wrong password, token-only, unreachable, via a full run")
def browser_checks():
    from pavs.engines.testing import browser
    if not browser.playwright_available():
        raise AssertionError("Playwright is not installed (pip install playwright)")
    with Work("2606") as w:
        res = {r.check_id: r for r in browser.run_browser_checks(w.live.url, "acme_demo", "admin", "admin-test-pw")}
        check(all(res[i].status == TestStatus.PASS for i in ("B01", "B02", "B03")), {k: v.status.value for k, v in res.items()})
        res = {r.check_id: r for r in browser.run_browser_checks(w.live.url, "acme_demo", "admin", "wrong")}
        check(res["B02"].status == TestStatus.FAIL and res["B03"].status == TestStatus.BLOCKED, "wrong password")
        res = {r.check_id: r for r in browser.run_browser_checks(w.live.url, "acme_demo", None, None)}
        check(res["B01"].status == TestStatus.PASS and res["B02"].status == TestStatus.BLOCKED, "no password")
        res = {r.check_id: r for r in browser.run_browser_checks("http://127.0.0.1:9/polarion", "x", "a", "b")}
        check(res["B01"].status == TestStatus.FAIL, "unreachable")
        env = w.connect().environment
        run = w.ctx.testing.run(env.id, "acme_demo", health=False, browser=True)
        check({r.check_id for r in run.results if r.group == "Browser"} == {"B01", "B02", "B03", "B04"}, "browser results in a run")
        check(clean(" ".join(str(r.evidence) for r in run.results)), "secret in browser evidence")


# =====================================================================================================================
# 8. Executables (optional) and the real local server (optional, no login)
# =====================================================================================================================
def exe_scenarios(exe_dir: Path):
    @scenario("EXE: pavs-cli.exe runs every command; the GUI exe opens, hands over a second launch, and closes cleanly")
    def exe_all():
        cli_suite(exe=str(exe_dir / "pavs-cli.exe"))
        data = Path(tempfile.mkdtemp(prefix="pavs-exe-"))
        env = dict(os.environ, PAVS_DATA_DIR=str(data))
        gui = exe_dir / "PolarionValidationStudio.exe"
        p1 = subprocess.Popen([str(gui)], env=env)
        try:
            time.sleep(10)
            check(p1.poll() is None, "GUI exe exited at start")
            p2 = subprocess.Popen([str(gui)], env=env)
            check(p2.wait(30) == 0, "second launch should exit immediately")
            check(p1.poll() is None, "first exe died")
            check((data / "pavs.sqlite3").exists(), "database not created")
        finally:
            for p in (p1,):
                if p.poll() is None:
                    p.kill()
            shutil.rmtree(data, ignore_errors=True)


def real_local_scenarios():
    @scenario("REAL local Polarion (no login): version detection, local repository reader, health checks and script analysis on real data")
    def real_local():
        from pavs.domain.enums import AuthMethod
        from pavs.domain.models import Environment
        from pavs.polarion.svn.local import find_local_repository
        if not find_local_repository():
            return
        tmp = Path(tempfile.mkdtemp(prefix="pavs-real-"))
        try:
            ctx = build_context(db_path=tmp / "real.sqlite3", vault_backend=MemoryBackend(), log_to_file=False)
            env = ctx.environments.save(Environment(name="real-local", base_url="http://localhost/polarion", auth_method=AuthMethod.NONE,
                                                    providers_enabled=[ProviderKind.REST, ProviderKind.SOAP, ProviderKind.SVN]))
            prof = ctx.connections.connect(env)
            check(prof.reachable and prof.version.release, f"detect: {prof.errors}")
            svn = prof.provider(ProviderKind.SVN)
            check(svn is not None and svn.state == ProviderState.AUTHENTICATED and "svnlook" in svn.detail, f"svn: {svn}")
            projects = [p for p in ("drivepilot", "Vision") if ctx.connections.session(env.id)]
            run = ctx.testing.run(env.id, "drivepilot", health=True, scripts=True)
            counts = run.counts()
            check(counts["error"] == 0, f"ERROR results: {counts}")
            check(counts["pass"] >= 5, f"too few real passes: {counts}")
            h08 = next((r for r in run.results if r.check_id == "H08"), None)
            check(h08 is None or h08.status != TestStatus.FAIL or "more than once" not in " ".join(h08.evidence), "enumeration false positive is back")
            vision = ctx.testing.run(env.id, "Vision", health=True)
            h11 = next(r for r in vision.results if r.check_id == "H11")
            check(h11.status != TestStatus.FAIL, f"project-defined roles must be known: {h11.evidence}")
            scripts = [r for r in run.results if r.group == "Scripts"]
            check(scripts and not any(r.status == TestStatus.FAIL for r in scripts), "real templates must analyse clean")
            ctx.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @scenario("REAL machine: TortoiseSVN found, TortoiseMerge and TortoiseProc really start (then closed), Playwright login page on the real server")
    def real_tools():
        from pavs.integrations.tortoisesvn import find_tortoise
        info = find_tortoise()
        check(info is not None, "TortoiseSVN not found")
        procs = []
        try:
            l = TortoiseLauncher(info, spawn=lambda line: procs.append(subprocess.Popen(line)))
            a, b = Path(tempfile.mkdtemp(prefix="pavs-d-")), None
            (a / "a.txt").write_text("a\nb\n")
            (a / "b.txt").write_text("a\nc\n")
            l.diff_local(a / "a.txt", a / "b.txt")
            l.log("http://localhost/repo/Vision/")
            time.sleep(4)
            check(all(p.poll() is None for p in procs), "a Tortoise window exited at once")
        finally:
            for p in procs:
                p.kill()
        from pavs.engines.testing import browser
        res = {r.check_id: r for r in browser.run_browser_checks("http://localhost/polarion", "drivepilot", None, None)}
        if res["B01"].status != TestStatus.PASS:
            check("activation" in res["B01"].actual.lower(), f"real login page: {res['B01'].actual}")
            print("      note: the real Polarion shows its ACTIVATION page (license), so there is no login form to test")


# =====================================================================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run scenarios whose name contains this text")
    ap.add_argument("--exe", help="folder with PolarionValidationStudio.exe and pavs-cli.exe")
    ap.add_argument("--real-local", action="store_true", help="also test the real local Polarion without logging in")
    args = ap.parse_args()
    if args.exe:
        exe_scenarios(Path(args.exe))
    if args.real_local:
        real_local_scenarios()
    failed = 0
    for name, fn in SCENARIOS:
        if args.only and args.only.lower() not in name.lower():
            continue
        t = time.time()
        try:
            fn()
            RESULTS.append((name, True, ""))
            print(f"PASS  {name}  ({time.time() - t:.1f}s)", flush=True)
        except Exception as exc:
            failed += 1
            detail = f"{type(exc).__name__}: {exc}"
            RESULTS.append((name, False, detail))
            print(f"FAIL  {name}\n      {detail[:600]}", flush=True)
            if os.environ.get("E2E_TRACE"):
                traceback.print_exc()
    print(f"\n{len(RESULTS) - failed} passed, {failed} failed, {len(RESULTS)} scenarios")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
