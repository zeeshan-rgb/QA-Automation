"""Headless command line: the same engines as the desktop app, for scheduled runs and build pipelines.

    pavs-cli scripts <folder> [--release 2410] [--out report.html]      offline script/template analysis
    pavs-cli projects --url <url>                                       connect and list projects
    pavs-cli run --url <url> --project <id> [--data] [--scripts] ...    read-only tests, report, exit code
    pavs-cli compat --url <url> [--out compat.md|compat.json]           what this server supports (live)
    pavs-cli merge-check --url <url> --project A --target-project B     what would arrive in B if A is merged into it

Secrets are never taken from the command line (they would show up in process lists and shell history).
Set PAVS_USER and PAVS_PASSWORD and/or PAVS_TOKEN (a Personal Access Token or a JWT) in the environment.

Exit codes: 0 passed (or passed with gaps), 1 a check failed (or gaps with --strict), 2 could not run
(connection, sign-in, role or usage problem).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .app import AppContext, build_context
from .domain.enums import EnvironmentKind
from .domain.errors import PavsError
from .infra.redaction import scrub
from .services.roles import RoleDenied

EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


def _out(text: str = "") -> None:
    sys.stdout.write(scrub(text) + "\n")


def _err(text: str) -> None:
    sys.stderr.write(scrub(text) + "\n")


def _credentials() -> tuple[str, str, str]:
    return (os.environ.get("PAVS_USER", "").strip(), os.environ.get("PAVS_PASSWORD", ""),
            os.environ.get("PAVS_TOKEN", "").strip())


def _connect(ctx: AppContext, args):
    user, password, token = _credentials()
    outcome = ctx.connections.quick_connect(
        args.url, user, password, token, read_config_files=not args.no_config_files,
        verify_tls=not args.insecure, kind=EnvironmentKind(args.kind),
        progress=lambda msg, pct: None)
    if not outcome.ok:
        raise PavsError(outcome.message)
    return outcome


def _print_run(run) -> None:
    c = run.counts()
    _out(f"{run.verdict}: {run.total} checks on {run.project_id} (Polarion {run.release or 'unknown'}): "
         f"{c['pass']} pass, {c['fail']} fail, {c['partial_pass']} partial, {c['blocked']} blocked, "
         f"{c['not_testable']} not testable, {c['error']} error. Coverage {run.coverage_pct}%.")
    for r in run.results:
        if r.status.value in ("fail", "error"):
            _out(f"  {r.status.value.upper():<6} {r.check_id:<6} {r.name}")
    for gap in run.inspection_gaps:
        _out(f"  could not inspect: {gap}")


def _exit_for(run, strict: bool) -> int:
    if run.verdict == "FAILED":
        return EXIT_FAILED
    if strict and run.verdict == "PASSED WITH GAPS":
        return EXIT_FAILED
    return EXIT_OK


def _export(ctx: AppContext, run, out: str | None) -> None:
    if out:
        path = ctx.testing.export(run.id, out)
        _out(f"Report saved: {path}")


# ------------------------------------------------------------------ commands
def cmd_scripts(ctx: AppContext, args) -> int:
    run = ctx.testing.analyze_local_scripts(args.folder, args.release)
    _print_run(run)
    _export(ctx, run, args.out)
    return _exit_for(run, args.strict)


def cmd_projects(ctx: AppContext, args) -> int:
    outcome = _connect(ctx, args)
    _out(outcome.message)
    for p in outcome.profile.projects:
        _out(f"  {p.id}\t{p.name}")
    return EXIT_OK


def cmd_run(ctx: AppContext, args) -> int:
    outcome = _connect(ctx, args)
    _out(outcome.message)
    set_id = None
    if args.requirements:
        analysis = ctx.testing.import_path(args.requirements, outcome.environment.id, args.project)
        set_id = analysis.requirement_set.id
        _out(f"Requirements: {len(analysis.requirements)} imported, {len(analysis.cases)} checks generated.")
    run = ctx.testing.run(outcome.environment.id, args.project, health=not args.no_health, set_id=set_id,
                          data=args.data, scripts=args.scripts, script_folder=args.script_folder, browser=args.browser)
    _print_run(run)
    _export(ctx, run, args.out)
    previous = ctx.testing.previous_run(run)
    if previous is not None:
        from .services.regression import summarize
        s = summarize(ctx.testing.compare_runs(previous.id, run.id))
        _out(f"Since the previous run: {s.get('regression', 0)} worse, {s.get('fixed', 0)} fixed.")
    return _exit_for(run, args.strict)


def cmd_compat(ctx: AppContext, args) -> int:
    outcome = _connect(ctx, args)
    profile = outcome.profile
    rows = [{"id": c.capability_id, "area": c.area, "title": c.title, "status": c.status.value,
             "verification": c.verification.value, "provider": c.provider.value if c.provider else "",
             "detail": c.detail} for c in profile.capabilities]
    summary = {"application": f"{APP_NAME} {__version__}", "server": outcome.environment.base_url if outcome.environment else args.url,
               "release": profile.version.release, "adapter": profile.adapter,
               "providers": {p.kind.value: p.state.value for p in profile.providers},
               "checked_at": profile.connected_at.isoformat(), "capabilities": rows}
    verified = sum(1 for r in rows if r["verification"] == "verified")
    _out(f"Polarion {summary['release'] or 'unknown'} (adapter {summary['adapter']}): {len(rows)} capabilities, "
         f"{verified} verified by a real call.")
    if args.out:
        path = Path(args.out)
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        else:
            lines = [f"# Compatibility record: Polarion {summary['release'] or 'unknown'}", "",
                     f"Adapter **{summary['adapter']}**. Providers: " + ", ".join(f"{k} {v}" for k, v in summary["providers"].items()),
                     f"Checked {summary['checked_at']} by {summary['application']}.", "",
                     "| Area | Capability | Status | Verification | Provider |", "|---|---|---|---|---|"]
            lines += [f"| {r['area']} | {r['title']} | {r['status']} | {r['verification']} | {r['provider']} |" for r in rows]
            path.write_text(scrub("\n".join(lines)) + "\n", encoding="utf-8")
        _out(f"Compatibility record saved: {path}")
    return EXIT_OK


def cmd_merge_check(ctx: AppContext, args) -> int:
    outcome = _connect(ctx, args)
    target_env = outcome.environment.id
    if args.target_url:
        user, password, token = (os.environ.get("PAVS_TARGET_USER", "").strip(), os.environ.get("PAVS_TARGET_PASSWORD", ""),
                                 os.environ.get("PAVS_TARGET_TOKEN", "").strip())
        user, password, token = user or _credentials()[0], password or _credentials()[1], token or _credentials()[2]
        other = ctx.connections.quick_connect(args.target_url, user, password, token, read_config_files=not args.no_config_files,
                                              verify_tls=not args.insecure, kind=EnvironmentKind(args.kind))
        if not other.ok:
            raise PavsError(other.message)
        target_env = other.environment.id
    rep = ctx.merge.compare(outcome.environment.id, args.project, target_env, args.target_project)
    c = rep.counts()
    _out(f"{rep.source_label} -> {rep.target_label}: {c['added']} new, {c['changed']} changed, {c['removed']} only in the target.")
    for fd, finding in rep.blockers:
        _out(f"  WOULD BREAK  {fd.path}:{finding.line}  {finding.rule} {finding.message}")
    for note in rep.notes:
        _out(f"  note: {note}")
    if args.out:
        _out(f"Report saved: {ctx.testing.export(rep.run_id, args.out)}")
    _out("Ready to merge in TortoiseSVN." if rep.ready else "Fix the problems above in the source before merging.")
    return EXIT_OK if rep.ready else EXIT_FAILED


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pavs-cli", description=f"{APP_NAME} {__version__}: headless test runs. No AI is used.",
                                epilog="Secrets: set PAVS_USER / PAVS_PASSWORD / PAVS_TOKEN in the environment.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def server(sp):
        sp.add_argument("--url", required=True, help="Polarion address, e.g. https://polarion.example.com/polarion")
        sp.add_argument("--kind", default="development", choices=[k.value for k in EnvironmentKind])
        sp.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
        sp.add_argument("--no-config-files", action="store_true", help="do not read the repository configuration (SVN)")

    s = sub.add_parser("scripts", help="analyse a folder of .js/.vm files offline")
    s.add_argument("folder")
    s.add_argument("--release", help="target Polarion release, e.g. 2410")
    s.add_argument("--out", help="report file (.xlsx .pdf .html .csv)")
    s.add_argument("--strict", action="store_true", help="exit 1 also when there are gaps")
    s.set_defaults(func=cmd_scripts, offline=True)

    s = sub.add_parser("projects", help="connect and list the projects")
    server(s)
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("run", help="run read-only tests on one project")
    server(s)
    s.add_argument("--project", required=True)
    s.add_argument("--no-health", action="store_true", help="skip the built-in health checks")
    s.add_argument("--data", action="store_true", help="add the Work Item data checks")
    s.add_argument("--scripts", action="store_true", help="add the script and template analysis")
    s.add_argument("--script-folder", help="also analyse this local folder")
    s.add_argument("--browser", action="store_true", help="add the browser checks (needs Playwright and Edge or Chrome)")
    s.add_argument("--requirements", help="a requirements file to generate and run checks from")
    s.add_argument("--out", help="report file (.xlsx .pdf .html .csv)")
    s.add_argument("--strict", action="store_true", help="exit 1 also when there are gaps")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("compat", help="record what this server supports (live capability detection)")
    server(s)
    s.add_argument("--out", help="record file (.md or .json)")
    s.set_defaults(func=cmd_compat)
    s = sub.add_parser("merge-check", help="compare two projects' configuration and check what would arrive (read-only)")
    server(s)
    s.add_argument("--project", required=True, help="source project (merge FROM)")
    s.add_argument("--target-project", required=True, help="target project (merge INTO)")
    s.add_argument("--target-url", help="target server, if different (credentials: PAVS_TARGET_USER/PASSWORD/TOKEN, else the same)")
    s.add_argument("--out", help="report file (.xlsx .pdf .html .csv)")
    s.set_defaults(func=cmd_merge_check)
    return p


def main(argv: list[str] | None = None, ctx: AppContext | None = None) -> int:
    args = build_parser().parse_args(argv)
    own = ctx is None
    ctx = ctx or build_context()
    try:
        return args.func(ctx, args)
    except RoleDenied as exc:
        _err(f"Not allowed: {exc}")
        return EXIT_ERROR
    except (PavsError, OSError, ValueError) as exc:
        _err(f"Error: {exc}")
        return EXIT_ERROR
    finally:
        if own:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
