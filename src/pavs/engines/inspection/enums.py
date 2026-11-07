"""Reading one enumeration through REST, SOAP or SVN (with Polarion's precedence)."""
from __future__ import annotations

from typing import Any

from ...domain.config import EnumOptionDef, Scope, Source
from ...domain.enums import ProviderKind
from ...polarion.soap.client import as_list
from .base import Collector, InspectionContext, src_rest, src_soap
from .parsers import parse_enum_options

# enumeration context -> config folder (relative to .polarion)
CONTEXT_DIRS = {None: "tracker/fields", "documents": "documents/fields",
                "testing": "testing/configuration", "plans": "planning/configuration"}


def split_enum_id(enum_id: str) -> tuple[str | None, str]:
    """'documents/document-status' -> ('documents', 'document-status'); 'status' -> (None, 'status')."""
    if "/" in enum_id:
        c, n = enum_id.split("/", 1)
        return c, n
    return None, enum_id


def svn_enum_candidates(enum_id: str, target_type: str | None) -> list[tuple[Scope, str]]:
    """Lookup order: project type-specific > project generic > global type-specific > global generic."""
    context, name = split_enum_id(enum_id)
    d = CONTEXT_DIRS.get(context, f"{context}/fields")
    order = []
    for scope in (Scope.PROJECT, Scope.GLOBAL):
        if target_type:
            order.append((scope, f"{d}/{target_type}-{name}-enum.xml"))
        order.append((scope, f"{d}/{name}-enum.xml"))
    return order


def rest_option(o: dict[str, Any]) -> EnumOptionDef:
    rules = []
    for r in o.get("linkRules") or []:
        for f in r.get("fromTypes") or ["*"]:
            for t in r.get("toTypes") or ["*"]:
                rules.append((f, t))
    known = {"id", "name", "description", "default", "hidden", "iconURL", "color", "oppositeName",
             "parent", "terminal", "linkRules"}
    return EnumOptionDef(id=o.get("id", ""), name=o.get("name", ""), description=o.get("description", ""),
                         default=bool(o.get("default")), hidden=bool(o.get("hidden")),
                         icon=o.get("iconURL", ""), color=o.get("color", ""),
                         opposite_name=o.get("oppositeName", ""), parent=bool(o.get("parent")),
                         terminal=bool(o.get("terminal")), rules=rules,
                         extra={k: v for k, v in o.items() if k not in known})


def unique_options(opts: list[EnumOptionDef]) -> list[EnumOptionDef]:
    """getAllEnumOptionsForId lists the options of EVERY context of an enumeration (each Work Item type or document type
    with its own variant), so the same option id comes back several times. Keep the first of each id."""
    seen: set[str] = set()
    out = []
    for o in opts:
        if o.id not in seen:
            seen.add(o.id)
            out.append(o)
    return out


def soap_option(o: Any) -> EnumOptionDef | None:
    if not isinstance(o, dict):
        return None
    seq = o.get("sequenceNumber")
    return EnumOptionDef(id=o.get("id", ""), name=o.get("name", "") or "",
                         default=o.get("default") == "true", hidden=o.get("hidden") == "true",
                         sort_order=int(seq) if isinstance(seq, str) and seq.lstrip("-").isdigit() else None)


def read_enum(ctx: InspectionContext, col: Collector, enum_id: str, target_type: str | None = None,
              prefer: tuple[ProviderKind, ...] = (ProviderKind.REST, ProviderKind.SOAP, ProviderKind.SVN)
              ) -> tuple[list[EnumOptionDef], Source] | None:
    """Effective options of one enumeration, or None (reasons recorded on the collector)."""
    context, name = split_enum_id(enum_id)
    for provider in prefer:
        if provider == ProviderKind.REST and ctx.rest_has(
                "GET /projects/{projectId}/enumerations/{enumContext}/{enumName}/{targetType}"):
            path = f"/projects/{ctx.project_id}/enumerations/{context or '~'}/{name}/{target_type or '~'}"
            body = col.attempt(f"REST {path}", lambda: ctx.rest.get(path))
            if body is not None:
                opts = ((body.get("data") or {}).get("attributes") or {}).get("options") or []
                col.used(ProviderKind.REST)
                return [rest_option(o) for o in opts], src_rest(path)
        elif provider == ProviderKind.SOAP and ctx.soap is not None and not target_type:
            # SOAP has no target-type parameter: only use it for generic enumerations
            resp = col.attempt(f"SOAP getAllEnumOptionsForId({enum_id})", lambda: ctx.soap.call(
                "Tracker", "getAllEnumOptionsForId", {"projectID": ctx.project_id, "enumID": enum_id}))
            if resp is not None:
                opts = unique_options([o for o in (soap_option(x) for x in as_list(resp)) if o and o.id])
                col.used(ProviderKind.SOAP)
                return opts, src_soap(f"Tracker.getAllEnumOptionsForId({enum_id})")
        elif provider == ProviderKind.SVN and ctx.repo is not None:
            for scope, rel in svn_enum_candidates(enum_id, target_type):
                found = col.attempt(f"SVN {rel}", lambda s=scope, r=rel: ctx.repo.file(s, ctx.project_id, r))
                if found:
                    text, source = found
                    col.used(ProviderKind.SVN)
                    return parse_enum_options(text, source.location), source
    return None
