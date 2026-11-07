"""Dashboard: environments, detected versions and totals from the latest test run of each project."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from ...app import AppContext
from ..widgets import Card, heading, item, make_table, pretty


class DashboardPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.c_envs = Card("Saved environments")
        self.c_conn = Card("Reachable on last check")
        self.c_req = Card("Requirements", "0", "Distinct requirements in the latest runs")
        self.c_tests = Card("Checks", "0", "Executed in the latest run of each project")
        self.c_pass = Card("Passed", "0")
        self.c_fail = Card("Failed", "0")
        self.c_blocked = Card("Blocked", "0")
        self.c_cov = Card("Coverage", "-", "Checks with a real verdict")
        grid = QGridLayout()
        for i, c in enumerate([self.c_envs, self.c_conn, self.c_req, self.c_tests,
                               self.c_pass, self.c_fail, self.c_blocked, self.c_cov]):
            grid.addWidget(c, i // 4, i % 4)

        self.table = make_table(["Environment", "Type", "Polarion", "Adapter", "REST", "SOAP", "SVN",
                                 "Projects", "Last check"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(heading("Dashboard", "Connected Polarion instances and validation totals."))
        lay.addLayout(grid)
        title = QLabel("Polarion instances")
        title.setObjectName("h1")
        lay.addSpacing(8)
        lay.addWidget(title)
        lay.addWidget(self.table, 1)
        self.refresh()

    def refresh(self) -> None:
        envs = self.ctx.environments.list()
        self.c_envs.set(str(len(envs)))
        reachable = 0
        self.table.setRowCount(len(envs))
        for r, env in enumerate(envs):
            prof = self.ctx.connections.latest_profile(env.id)
            if prof and prof.reachable:
                reachable += 1
            self.table.setItem(r, 0, item(env.name))
            self.table.setItem(r, 1, item(pretty(env.kind.value)))
            self.table.setItem(r, 2, item(prof.version.release or "unknown" if prof else "-"))
            self.table.setItem(r, 3, item(prof.adapter if prof else "-"))
            for c, kind in enumerate(("rest", "soap", "svn"), start=4):
                ps = next((p for p in prof.providers if p.kind.value == kind), None) if prof else None
                self.table.setItem(r, c, item(pretty(ps.state.value) if ps else "-",
                                              ps.state.value if ps else None, ps.detail if ps else None))
            self.table.setItem(r, 7, item(str(len(prof.projects)) if prof else "-"))
            self.table.setItem(r, 8, item(f"{prof.connected_at.astimezone():%Y-%m-%d %H:%M}" if prof else "never"))
        self.c_conn.set(f"{reachable}/{len(envs)}")
        self._refresh_totals()

    def _refresh_totals(self) -> None:
        """Totals come only from real stored runs: the newest run of each (environment, project)."""
        latest: dict[tuple[str, str], object] = {}
        for run in sorted(self.ctx.testing.history(), key=lambda r: r.started_at):
            latest[(run.environment_id, run.project_id)] = run
        counts = {"pass": 0, "fail": 0, "blocked": 0, "error": 0, "partial_pass": 0, "not_testable": 0}
        total = decided = 0
        reqs: set[tuple[str, str, str]] = set()
        for (env_id, proj), run in latest.items():
            for k, v in run.counts().items():
                counts[k] = counts.get(k, 0) + v
            total += run.total
            decided += sum(1 for r in run.results if r.status.value in ("pass", "fail", "partial_pass"))
            reqs.update((env_id, proj, r.requirement_id) for r in run.results if r.requirement_id)
        self.c_req.set(str(len(reqs)))
        self.c_tests.set(str(total))
        self.c_pass.set(str(counts["pass"]))
        self.c_fail.set(str(counts["fail"] + counts["error"]))
        self.c_blocked.set(str(counts["blocked"] + counts["not_testable"]))
        self.c_cov.set(f"{100.0 * decided / total:.0f}%" if total else "-")
