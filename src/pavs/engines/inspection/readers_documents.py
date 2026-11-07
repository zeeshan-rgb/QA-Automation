"""Documents (LiveDocs), document types/statuses, and pages."""
from __future__ import annotations

from ...domain.config import DocumentInfo, PageInfo
from ...domain.enums import ProviderKind
from ...polarion.soap.client import as_list
from .base import Collector, InspectionContext, first_text
from .enums import read_enum

MAX_DOCUMENTS = 2000


class DocumentsReader:
    area = "documents"
    title = "Documents"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        docs: list[DocumentInfo] = []
        rest_ok = ctx.rest_has("GET /projects/{projectId}/documents")
        if rest_ok:
            path = f"/projects/{ctx.project_id}/documents"
            items = col.attempt(f"REST {path}", lambda: list(ctx.rest.iterate(
                path, params={"fields[documents]": "title,type,status,moduleFolder,moduleName"},
                page_size=100, max_items=MAX_DOCUMENTS)))
            if items is not None:
                for it in items:
                    a = it.get("attributes") or {}
                    parts = (it.get("id") or "").split("/")
                    docs.append(DocumentInfo(space=a.get("moduleFolder") or (parts[1] if len(parts) > 2 else ""),
                                             name=a.get("moduleName") or (parts[-1] if parts else ""),
                                             title=a.get("title", ""), type=a.get("type", ""),
                                             status=a.get("status", ""), uri=it.get("id", "")))
                col.used(ProviderKind.REST)
                col.ok()
        if not docs and ctx.soap is not None:
            resp = col.attempt("SOAP queryModules", lambda: ctx.soap.call("Tracker", "queryModules", {
                "query": f"project.id:{ctx.project_id}", "sort": "moduleFolder",
                "fields": ["id", "title", "type", "status", "moduleFolder", "moduleName"],
                "resultsLimit": MAX_DOCUMENTS}))
            if resp is not None:
                for m in as_list(resp):
                    if not isinstance(m, dict):
                        continue
                    docs.append(DocumentInfo(space=first_text(m.get("moduleFolder")),
                                             name=first_text(m.get("moduleName")) or first_text(m.get("id")),
                                             title=first_text(m.get("title")), type=first_text(m.get("type")),
                                             status=first_text(m.get("status")), uri=m.get("uri", "")))
                col.used(ProviderKind.SOAP)
                col.ok()
        if not docs and not col.result.errors and not (rest_ok or ctx.soap):
            col.gap("Document list", "REST document listing needs 2512+ and SOAP is not signed in",
                    "Sign in to SOAP, or use a 2512+ server with REST.")
        if len(docs) >= MAX_DOCUMENTS:
            col.note(f"Only the first {MAX_DOCUMENTS} Documents are listed.")

        types = read_enum(ctx, col, "documents/document-type")
        statuses = read_enum(ctx, col, "documents/document-status")
        if types:
            col.result.put("document_types", types[0])
            ctx.shared["document_types"] = [o.id for o in types[0]]
            col.ok()
        else:
            col.gap("Document types", "the documents/document-type enumeration could not be read")
        if statuses:
            col.result.put("document_statuses", statuses[0])
            col.ok()
        col.result.put("documents", sorted(docs, key=lambda d: (d.space, d.name)))
        return col.finish()


class PagesReader:
    area = "pages"
    title = "Pages & reports"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        pages: list[PageInfo] = []
        if ctx.rest_has("GET /projects/{projectId}/pages"):
            path = f"/projects/{ctx.project_id}/pages"
            items = col.attempt(f"REST {path}", lambda: list(ctx.rest.iterate(
                path, params={"fields[pages]": "title,pageName,spaceId"}, page_size=100, max_items=5000)))
            if items is not None:
                for it in items:
                    a = it.get("attributes") or {}
                    pages.append(PageInfo(space=a.get("spaceId", ""), name=a.get("pageName", ""),
                                          title=a.get("title", ""), kind="page"))
                col.used(ProviderKind.REST)
                col.ok()
        if not pages and ctx.soap is not None:
            spaces = col.attempt("SOAP getWikiSpaces", lambda: ctx.soap.call(
                "Tracker", "getWikiSpaces", {"projectId": ctx.project_id}))
            for space in [s for s in as_list(spaces) if isinstance(s, str)] if spaces is not None else []:
                resp = col.attempt(f"SOAP getWikiPages({space})", lambda sp=space: ctx.soap.call(
                    "Tracker", "getWikiPages", {"projectId": ctx.project_id, "spaceId": sp}))
                for p in as_list(resp) if resp is not None else []:
                    if isinstance(p, dict):
                        pages.append(PageInfo(space=space, name=first_text(p.get("pageName")),
                                              title=first_text(p.get("title")), kind=first_text(p.get("type"))))
            if spaces is not None:
                col.used(ProviderKind.SOAP)
                col.ok()
                col.note("SOAP lists pages per wiki space; LiveReport pages may be missing on older releases.")
        if not pages and not col.result.errors and ctx.rest is None and ctx.soap is None:
            col.gap("Pages", "needs REST (2512+) or SOAP")
        col.result.put("pages", sorted(pages, key=lambda p: (p.space, p.name)))
        return col.finish()
