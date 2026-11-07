"""Workflow definitions (SVN) and per-item workflow actions (REST / SOAP, read-only sampling)."""
from __future__ import annotations

from ...domain.config import ItemWorkflowAction, Scope, WorkflowDef
from ...domain.enums import ProviderKind
from ...polarion.soap.client import as_list
from .base import REMEDY_SVN, Collector, InspectionContext, first_text
from .enums import read_enum
from .parsers import parse_workflow, workflow_file_target

WORKFLOW_DIRS = {"WorkItem": "tracker/workflow", "Module": "documents/workflow",
                 "TestRun": "testing/configuration/workflow"}
MAX_SAMPLED_TYPES = 15


class WorkflowReader:
    area = "workflow"
    title = "Workflows"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        if ctx.repo is not None:
            files, effective = self._definitions(ctx, col)
            col.result.put("files", files)
            col.result.put("effective", effective)
            ctx.shared["workflows"] = effective
        else:
            col.gap("Workflow definitions (statuses, transitions, conditions, functions)",
                    "not exposed by REST or SOAP", REMEDY_SVN)
        if ctx.sample_items:
            actions = self._sample_item_actions(ctx, col)
            col.result.put("item_actions", actions)
        return col.finish()

    # ------------------------------------------------------------------ definitions
    def _definitions(self, ctx: InspectionContext, col: Collector) -> tuple[list[WorkflowDef], list[WorkflowDef]]:
        files: list[WorkflowDef] = []
        for prototype, d in WORKFLOW_DIRS.items():
            for scope in (Scope.GLOBAL, Scope.PROJECT):
                listed = col.attempt(f"SVN {scope.value} {d}",
                                     lambda s=scope, dd=d: ctx.repo.files(s, ctx.project_id, dd, ".xml")) or []
                for name, text, source in listed:
                    try:
                        target = workflow_file_target(name)
                    except ValueError:
                        target = name[:-4]           # e.g. testing 'automated-workflow' variants
                    wf = col.attempt(f"parse {source.location}",
                                     lambda t=text, tg=target, sc=scope, so=source: parse_workflow(t, tg, sc, so))
                    if wf is not None:
                        if wf.prototype != prototype:
                            col.note(f"{source.location}: prototype {wf.prototype} in the {prototype} folder")
                        files.append(wf)
        col.used(ProviderKind.SVN)
        col.ok()

        # effective workflow per type: project+type > global+type > project generic > global generic (Help 0706)
        def pick(prototype: str, target: str | None) -> WorkflowDef | None:
            cands = [w for w in files if w.prototype == prototype]
            order = [(Scope.PROJECT, target), (Scope.GLOBAL, target), (Scope.PROJECT, None), (Scope.GLOBAL, None)]
            for scope, tg in order:
                hit = next((w for w in cands if w.scope == scope and w.target_type == tg), None)
                if hit:
                    return hit
            return None

        effective: list[WorkflowDef] = []
        wi_types = ctx.shared.get("types") or []
        for t in wi_types:
            wf = pick("WorkItem", t)
            if wf:
                effective.append(wf.model_copy(update={"target_type": t}))
        doc_types = self._document_types(ctx, col)
        for t in doc_types:
            wf = pick("Module", t)
            if wf:
                effective.append(wf.model_copy(update={"target_type": t}))
        for w in files:
            if w.prototype == "TestRun":
                effective.append(w)
        if not wi_types:
            col.note("Work Item types unknown: only the workflow files are listed.")
        return files, effective

    def _document_types(self, ctx: InspectionContext, col: Collector) -> list[str]:
        if "document_types" in ctx.shared:
            return ctx.shared["document_types"]
        found = read_enum(ctx, col, "documents/document-type")
        types = [o.id for o in found[0]] if found else []
        ctx.shared["document_types"] = types
        return types

    # ------------------------------------------------------------------ per item
    def _sample_item_actions(self, ctx: InspectionContext, col: Collector) -> list[ItemWorkflowAction]:
        types = (ctx.shared.get("types") or [])[:MAX_SAMPLED_TYPES]
        if not types:
            return []
        out: list[ItemWorkflowAction] = []
        rest_ok = ctx.rest is not None and ctx.rest_has(
            "GET /projects/{projectId}/workitems/{workItemId}/actions/getWorkflowActions")
        for t in types:
            if rest_ok:
                got = self._rest_item(ctx, col, t)
            elif ctx.soap is not None:
                got = self._soap_item(ctx, col, t)
            else:
                col.gap("Per-item workflow actions", "needs REST or SOAP")
                return out
            out += got
        if out:
            col.ok()
            col.note("Per-item actions come from one existing Work Item per type (read only, nothing is changed). "
                     "They show the actions available to the signed-in user right now.")
        return out

    def _rest_item(self, ctx: InspectionContext, col: Collector, t: str) -> list[ItemWorkflowAction]:
        items = col.attempt(f"REST sample {t}", lambda: list(ctx.rest.iterate(
            f"/projects/{ctx.project_id}/workitems",
            params={"query": f"type:{t}", "fields[workitems]": "id,status"}, page_size=1, max_items=1)))
        if not items:
            return []
        wid = (items[0].get("attributes") or {}).get("id") or items[0].get("id", "").split("/")[-1]
        status = (items[0].get("attributes") or {}).get("status", "")
        path = f"/projects/{ctx.project_id}/workitems/{wid}/actions/getWorkflowActions"
        body = col.attempt(f"REST {path}", lambda: ctx.rest.get(path))
        if body is None:
            return []
        col.used(ProviderKind.REST)
        return [ItemWorkflowAction(
            work_item=wid, work_item_type=t, current_status=status,
            action_id=a.get("nativeActionId") or a.get("id", ""), name=a.get("name", ""),
            available=bool(a.get("available")), target_status=a.get("targetStatus"),
            required_fields=a.get("requiredFields") or [], required_roles=a.get("requiredRoles") or [],
            signature_required=bool(a.get("isSignatureRequired")),
            unavailable_reason=a.get("unavailableReason", "")) for a in body.get("data") or []]

    def _soap_item(self, ctx: InspectionContext, col: Collector, t: str) -> list[ItemWorkflowAction]:
        uris = col.attempt(f"SOAP sample {t}", lambda: ctx.soap.call(
            "Tracker", "queryWorkItemUrisLimited",
            {"query": f"project.id:{ctx.project_id} AND type:{t}", "sort": "id", "resultsLimit": 1}))
        uri_list = [u for u in as_list(uris) if isinstance(u, str) and u] if uris is not None else []
        if not uri_list:
            return []
        uri = uri_list[0]
        wid = uri.rsplit("}", 1)[-1]
        out = []
        for op, available in (("getAvailableActions", True), ("getUnavailableActions", False)):
            resp = col.attempt(f"SOAP {op}", lambda o=op: ctx.soap.call("Tracker", o, {"workitemURI": uri}))
            for a in as_list(resp) if resp is not None else []:
                if not isinstance(a, dict):
                    continue
                req = a.get("requiredFeatures")
                out.append(ItemWorkflowAction(
                    work_item=wid, work_item_type=t, action_id=a.get("nativeActionId") or a.get("actionId", ""),
                    name=a.get("actionName", "") or "", available=available,
                    target_status=first_text(a.get("targetStatus")) or None,
                    required_fields=[x for x in (req if isinstance(req, list) else as_list(req)) if isinstance(x, str)],
                    unavailable_reason=first_text(a.get("unavailabilityMessage"))))
        col.used(ProviderKind.SOAP)
        return out
