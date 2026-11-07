"""Work Item types, link roles, custom fields, enumerations and UI field rules."""
from __future__ import annotations

from ...domain.config import EnumerationDef, FieldDef, Scope
from ...domain.enums import ProviderKind
from ...polarion.soap.client import as_list
from .base import (REMEDY_SVN, Collector, InspectionContext, first_text, src_rest, src_soap)
from .enums import read_enum, rest_option
from .parsers import (custom_fields_target, enum_file_parts, field_enum_id, hat_file_target,
                      parse_custom_fields, parse_enum_options, parse_form_filter, parse_read_only_fields)

WELL_KNOWN_ENUMS = ("status", "severity", "priority", "resolution", "workitem-type", "workitem-link-role",
                    "hyperlink-role", "documents/document-type", "documents/document-status")


class TypesReader:
    area = "types"
    title = "Work Item types"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        found = read_enum(ctx, col, "workitem-type")
        if found is None:
            col.gap("Work Item types", "no provider could read the workitem-type enumeration",
                    "Sign in to REST or SOAP, or enable the SVN reader.")
            ctx.shared["types"] = []
            return col.finish()
        options, source = found
        col.ok()
        col.result.put("types", options)
        col.result.notes.append(f"Read from {source.label()}")
        ctx.shared["types"] = [o.id for o in options]
        return col.finish()


class LinkRolesReader:
    area = "links"
    title = "Link roles"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        found = read_enum(ctx, col, "workitem-link-role")
        if found is None:
            col.gap("Link roles", "no provider could read the workitem-link-role enumeration")
            return col.finish()
        options, source = found
        col.ok()
        col.result.put("link_roles", options)
        if source.provider == ProviderKind.SOAP:
            col.note("SOAP returns ids and names only; opposite names and allowed type rules need REST or SVN.")
        col.result.notes.append(f"Read from {source.label()}")
        return col.finish()


class FieldsReader:
    area = "fields"
    title = "Custom fields"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        types = ctx.shared.get("types") or []
        effective: list[FieldDef] = []
        layers: list[FieldDef] = []

        # ---- effective definitions per type from the API
        rest_ok = ctx.rest_has("GET /projects/{projectId}/customfields/{resourceType}/{targetType}")
        if not types:
            col.gap("Effective custom fields", "Work Item types unknown (types section failed)")
        for t in types:
            got = False
            if rest_ok:
                path = f"/projects/{ctx.project_id}/customfields/workitems/{t}"
                body = col.attempt(f"REST {path}", lambda p=path: ctx.rest.get(p))
                if body is not None:
                    for f in ((body.get("data") or {}).get("attributes") or {}).get("fields") or []:
                        effective.append(self._rest_field(f, t, path))
                    col.used(ProviderKind.REST)
                    got = True
            if not got and ctx.soap is not None:
                resp = col.attempt(f"SOAP getDefinedCustomFieldTypes({t})", lambda tt=t: ctx.soap.call(
                    "Tracker", "getDefinedCustomFieldTypes", {"projectID": ctx.project_id, "typeID": tt}))
                if resp is not None:
                    for f in as_list(resp):
                        if isinstance(f, dict) and f.get("id"):
                            effective.append(self._soap_field(f, t))
                    col.used(ProviderKind.SOAP)
                    got = True
            if got:
                col.ok()
        if types and not effective and not col.result.errors and not (rest_ok or ctx.soap):
            col.gap("Effective custom fields", "REST custom-field endpoints (2512+) and SOAP are not available")

        # ---- configuration layers from SVN
        if ctx.repo is not None:
            for scope in (Scope.GLOBAL, Scope.PROJECT):
                for name, text, source in col.attempt(
                        f"SVN {scope.value} fields", lambda s=scope: ctx.repo.files(
                            s, ctx.project_id, "tracker/fields", "custom-fields.xml")) or []:
                    try:
                        target = custom_fields_target(name)
                    except ValueError:
                        continue
                    layers += parse_custom_fields(text, target, scope, source)
            col.used(ProviderKind.SVN)
            col.ok()
        else:
            col.gap("Custom field files (global vs project, per type)", "SVN reader not enabled", REMEDY_SVN)

        if not effective and layers:
            col.note("No API returned effective fields; the layer files below are the only source.")
        col.result.put("effective", effective)
        col.result.put("layers", layers)
        ctx.shared["field_enums"] = sorted({f.enum_id for f in effective + layers
                                            if f.enum_id and not f.enum_id.startswith("@")})
        return col.finish()

    @staticmethod
    def _rest_field(f: dict, target: str, path: str) -> FieldDef:
        kind = (f.get("type") or {}).get("kind", "")
        t = f.get("type") or {}
        enum_name = t.get("enumName") or t.get("enumId")
        type_str = f"enum:{enum_name}" if kind == "enumeration" and enum_name else kind
        if kind == "relationship" and t.get("targetResourceTypes"):
            type_str = "enum:@" + ",".join(t["targetResourceTypes"])
        return FieldDef(id=f.get("id", ""), name=f.get("name", ""), type=type_str,
                        enum_id=field_enum_id(type_str), required=bool(f.get("required")),
                        multi=bool(t.get("multiValued") or f.get("multi")),
                        default=f.get("defaultValue"), description=f.get("description", ""),
                        depends_on=f.get("dependsOn"), target_type=target, scope=Scope.EFFECTIVE,
                        sources=[src_rest(path)])

    @staticmethod
    def _soap_field(f: dict, target: str) -> FieldDef:
        qname = first_text(f.get("type"))
        enum_id = f.get("enumId") if isinstance(f.get("enumId"), str) else None
        type_str = f"enum:{enum_id}" if enum_id else qname.split(":")[-1]
        return FieldDef(id=f.get("id", ""), name=f.get("name", "") or "", type=type_str,
                        enum_id=enum_id or field_enum_id(type_str), required=f.get("required") == "true",
                        multi=f.get("multi") == "true", default=first_text(f.get("defaultValue")) or None,
                        description=first_text(f.get("description")), depends_on=f.get("dependsOn") or None,
                        target_type=target, scope=Scope.EFFECTIVE,
                        sources=[src_soap("Tracker.getDefinedCustomFieldTypes")])


class EnumerationsReader:
    area = "enumerations"
    title = "Enumerations"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        types = set(ctx.shared.get("types") or [])
        out: list[EnumerationDef] = []

        if ctx.repo is not None:
            out = self._from_svn(ctx, col, types)
        elif ctx.rest_has("GET /projects/{projectId}/enumerations"):
            path = f"/projects/{ctx.project_id}/enumerations"
            items = col.attempt(f"REST {path}", lambda: list(ctx.rest.iterate(path, page_size=100)))
            if items is not None:
                for it in items:
                    a = it.get("attributes") or {}
                    ctxname = a.get("enumContext")
                    enum_id = f"{ctxname}/{a.get('enumName')}" if ctxname and ctxname != "~" else a.get("enumName", "")
                    target = a.get("targetType")
                    out.append(EnumerationDef(enum_id=enum_id, target_type=None if target in (None, "", "~") else target,
                                              scope=Scope.EFFECTIVE, options=[rest_option(o) for o in a.get("options") or []],
                                              sources=[src_rest(path)]))
                col.used(ProviderKind.REST)
                col.ok()
        if not out:
            # last resort: well-known enumerations + those referenced by custom fields, one by one
            wanted = list(dict.fromkeys(list(WELL_KNOWN_ENUMS) + list(ctx.shared.get("field_enums") or [])))
            for enum_id in wanted:
                found = read_enum(ctx, col, enum_id)
                if found:
                    opts, source = found
                    out.append(EnumerationDef(enum_id=enum_id, scope=source.scope, options=opts, sources=[source]))
            if out:
                col.ok()
                col.gap("Complete list of enumerations",
                        "this server offers no listing (REST /enumerations needs 2512+); only well-known and "
                        "field-referenced enumerations were read", REMEDY_SVN)
            else:
                col.gap("Enumerations", "no provider available")
        col.result.put("enumerations", sorted(out, key=lambda e: (e.enum_id, e.target_type or "")))
        return col.finish()

    def _from_svn(self, ctx: InspectionContext, col: Collector, types: set[str]) -> list[EnumerationDef]:
        """All enum files, global and project, with project files winning per (enum, type)."""
        dirs = {None: "tracker/fields", "documents": "documents/fields", "testing": "testing/configuration",
                "plans": "planning/configuration"}
        found: dict[tuple[str, str | None], EnumerationDef] = {}
        for scope in (Scope.GLOBAL, Scope.PROJECT):         # project read last -> overrides
            for context, d in dirs.items():
                files = col.attempt(f"SVN {scope.value} {d}",
                                    lambda s=scope, dd=d: ctx.repo.files(s, ctx.project_id, dd, "-enum.xml")) or []
                for name, text, source in files:
                    target, enum = enum_file_parts(name, types)
                    enum_id = f"{context}/{enum}" if context else enum
                    opts = col.attempt(f"parse {name}", lambda t=text, l=source.location: parse_enum_options(t, l))
                    if opts is None:
                        continue
                    found[(enum_id, target)] = EnumerationDef(enum_id=enum_id, target_type=target, scope=scope,
                                                             options=opts, sources=[source])
        col.used(ProviderKind.SVN)
        col.ok()
        col.note("Enumerations from the configuration files; a project file replaces the global file "
                 "with the same name and type.")
        return list(found.values())


class FieldRulesReader:
    area = "field_rules"
    title = "Read-only fields & form filters"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        if ctx.repo is None:
            col.gap("Read-only fields and form filters",
                    "UI-only settings: not exposed by REST or SOAP", REMEDY_SVN)
            return col.finish()
        rules = []
        for scope in (Scope.GLOBAL, Scope.PROJECT):
            base = ctx.repo.base(scope, ctx.project_id)
            if base is None:
                continue
            for hat in [h.rstrip("/") for h in ctx.repo.list(f"{base}/hats") if h.endswith("/")]:
                for suffix, parser in (("read-only-fields.xml", parse_read_only_fields),
                                       ("form-filter.xml", parse_form_filter)):
                    files = col.attempt(f"SVN {hat} {suffix}", lambda s=scope, h=hat, sf=suffix:
                                        ctx.repo.files(s, ctx.project_id, f"hats/{h}/tracker", sf)) or []
                    for name, text, source in files:
                        try:
                            target = hat_file_target(name, suffix)
                        except ValueError:
                            continue
                        parsed = col.attempt(f"parse {name}", lambda t=text, tg=target, sc=scope, so=source:
                                             parser(t, tg, sc, so)) or []
                        for r in parsed:
                            r.view = hat
                        rules += parsed
        col.used(ProviderKind.SVN)
        col.ok()
        col.note("These rules only affect the Polarion UI; they are not enforced by the server or the APIs.")
        col.result.put("rules", rules)
        return col.finish()

