"""Read-only checks on the actual Work Items of a project (links, statuses). Never writes.

Items are read through SOAP (queryWorkItemsLimited, documented types) or REST (relationships of
`linkedWorkItems`, whose resource ids look like project/WI-1/role/project/WI-2).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ...domain.enums import TestStatus
from ...domain.errors import PavsError
from ...domain.testing import CheckResult, Severity
from ...polarion.soap.client import as_list
from .view import ConfigView

DEFAULT_LIMIT = 5000
LIST_LIMIT = 25


@dataclass
class Item:
    id: str
    type: str
    status: str
    links: list[tuple[str, str]] = field(default_factory=list)      # (role, target key)


@dataclass
class DataSet:
    items: dict[str, Item]
    truncated: bool
    source: str                         # "SOAP" | "REST"
    project: str

    def of_type(self, t: str) -> list[Item]:
        return [i for i in self.items.values() if i.type == t]

    def linked_pairs(self) -> set[tuple[str, str]]:
        """Undirected pairs (a, b) of local items that are linked in either direction."""
        out = set()
        for it in self.items.values():
            for _, tgt in it.links:
                out.add((it.id, tgt))
                out.add((tgt, it.id))
        return out


def _txt(v) -> str:
    while isinstance(v, dict):
        v = v.get("id") if "id" in v else (next(iter(v.values())) if len(v) == 1 else "")
    return v or ""


def _soap_target(uri: str, project: str) -> str | None:
    m = re.search(r"/default/(?P<p>[^$/]+)\$\{WorkItem\}(?P<i>.+)$", uri or "")
    if not m:
        return (uri or "").rsplit("}", 1)[-1] or None
    return m.group("i") if m.group("p") == project else f"{m.group('p')}/{m.group('i')}"


def load_items(session, project: str, limit: int = DEFAULT_LIMIT) -> DataSet:
    """Prefer SOAP (documented structure); fall back to REST. Raises PavsError if neither works."""
    errors = []
    if session.soap is not None:
        try:
            return _via_soap(session.soap, project, limit)
        except PavsError as exc:
            errors.append(f"SOAP: {exc}")
    if session.rest is not None:
        try:
            return _via_rest(session.rest, project, limit)
        except PavsError as exc:
            errors.append(f"REST: {exc}")
    raise PavsError("Work Items could not be read (" + ("; ".join(errors) or "no SOAP or REST sign-in") + ")")


def _via_soap(soap, project: str, limit: int) -> DataSet:
    resp = soap.call("Tracker", "queryWorkItemsLimited", {
        "query": f"project.id:{project}", "sort": "id",
        "fields": ["id", "type", "status", "linkedWorkItems"], "resultsLimit": limit + 1})
    items: dict[str, Item] = {}
    for w in as_list(resp):
        if not isinstance(w, dict) or not w.get("id"):
            continue
        links = []
        raw = w.get("linkedWorkItems")
        for lw in (as_list(raw.get("LinkedWorkItem") if isinstance(raw, dict) and "LinkedWorkItem" in raw else raw)
                   if raw else []):
            if isinstance(lw, dict):
                tgt = _soap_target(lw.get("workItemURI", ""), project)
                if tgt:
                    links.append((_txt(lw.get("role")), tgt))
        items[w["id"]] = Item(w["id"], _txt(w.get("type")), _txt(w.get("status")), links)
    truncated = len(items) > limit
    if truncated:
        items = dict(list(items.items())[:limit])
    return DataSet(items, truncated, "SOAP", project)


def _via_rest(rest, project: str, limit: int) -> DataSet:
    items: dict[str, Item] = {}
    for r in rest.iterate(f"/projects/{project}/workitems",
                          params={"fields[workitems]": "type,status,linkedWorkItems", "sort": "id"},
                          page_size=100, max_items=limit + 1):
        a = r.get("attributes") or {}
        wid = (r.get("id") or "").split("/")[-1]
        links = []
        for ref in (((r.get("relationships") or {}).get("linkedWorkItems") or {}).get("data") or []):
            parts = (ref.get("id") or "").split("/")
            if len(parts) == 5:
                links.append((parts[2], parts[4] if parts[3] == project else f"{parts[3]}/{parts[4]}"))
        items[wid] = Item(wid, a.get("type", ""), a.get("status", ""), links)
    truncated = len(items) > limit
    if truncated:
        items = dict(list(items.items())[:limit])
    return DataSet(items, truncated, "REST", project)


# ---------------------------------------------------------------- coverage (used by the traceability check)
def coverage(ds: DataSet, from_type: str, to_type: str) -> tuple[list[Item], list[Item]]:
    """(covered, uncovered) items of `from_type` that are linked, in either direction, to an item of `to_type`."""
    pairs = ds.linked_pairs()
    targets = {i.id for i in ds.of_type(to_type)}
    covered, uncovered = [], []
    for it in ds.of_type(from_type):
        (covered if any((it.id, t) in pairs for t in targets) else uncovered).append(it)
    return covered, uncovered


# ---------------------------------------------------------------- data health checks D01-D03
def _trim(items: list[str]) -> list[str]:
    return items[:LIST_LIMIT] + ([f"... and {len(items) - LIST_LIMIT} more"] if len(items) > LIST_LIMIT else [])


def run_data_checks(view: ConfigView, ds: DataSet) -> list[CheckResult]:
    out: list[CheckResult] = []
    note = [f"Read {len(ds.items)} Work Items via {ds.source}" + (" (limit reached: only these were checked)" if ds.truncated else "")]
    started = time.perf_counter()

    # D01 statuses used by items exist in their workflow
    errs = []
    wf_ok = view.has_data("workflow", "effective")
    if not wf_ok:
        out.append(CheckResult(check_id="D01", group="Data checks", name="Work Items use valid statuses", status=TestStatus.BLOCKED,
                               expected="every Work Item status exists in its type's workflow",
                               actual="not checked: workflow definitions could not be read", evidence=[view.why_missing("workflow")] + note))
    else:
        for it in ds.items.values():
            known = {o["id"] for o in view.statuses(it.type)}
            if known and it.status and it.status not in known:
                errs.append(f"{it.id} ({it.type}) has status '{it.status}' which its workflow does not define")
        out.append(CheckResult(check_id="D01", group="Data checks", name="Work Items use valid statuses",
                               status=TestStatus.FAIL if errs else TestStatus.PASS, severity=Severity.ERROR if errs else Severity.INFO,
                               expected="every Work Item status exists in its type's workflow",
                               actual=f"{len(errs)} Work Item(s) with an undefined status" if errs else "all statuses are valid",
                               mechanism=f"Work Items ({ds.source})", evidence=_trim(errs) + note))

    # D02 broken links (target in this project that was not found)
    errs = []
    if not ds.truncated:
        for it in ds.items.values():
            for role, tgt in it.links:
                if "/" not in tgt and tgt not in ds.items:
                    errs.append(f"{it.id} --{role}--> {tgt}: the target does not exist")
    out.append(CheckResult(check_id="D02", group="Data checks", name="Links point to existing Work Items",
                           status=TestStatus.BLOCKED if ds.truncated else TestStatus.FAIL if errs else TestStatus.PASS,
                           severity=Severity.ERROR if errs else Severity.INFO, expected="no link refers to a missing Work Item",
                           actual="not checked: too many Work Items to load all targets" if ds.truncated else
                           (f"{len(errs)} broken link(s)" if errs else "no broken links"), mechanism=f"Work Items ({ds.source})",
                           evidence=_trim(errs) + note))

    # D03 orphans: items without any link, by type (warning only)
    pairs = ds.linked_pairs()
    linked = {a for a, _ in pairs}
    orphans: dict[str, list[str]] = {}
    for it in ds.items.values():
        if it.id not in linked and it.type != "heading":
            orphans.setdefault(it.type, []).append(it.id)
    total = sum(len(v) for v in orphans.values())
    ev = [f"{t}: {len(v)} unlinked (e.g. {', '.join(v[:5])})" for t, v in sorted(orphans.items())]
    out.append(CheckResult(check_id="D03", group="Data checks", name="Work Items are linked to other items",
                           status=TestStatus.PARTIAL_PASS if total else TestStatus.PASS,
                           severity=Severity.WARNING if total else Severity.INFO,
                           expected="no Work Item without any link (orphan)",
                           actual=f"{total} unlinked Work Item(s)" if total else "every Work Item has at least one link",
                           mechanism=f"Work Items ({ds.source})", evidence=ev + note))
    ms = round((time.perf_counter() - started) * 1000, 1)
    for r in out:
        r.duration_ms = ms
    return out
