"""Project and user discovery (Phase 1). Uses REST when possible, SOAP otherwise."""
from __future__ import annotations

from typing import Any

from ..domain.models import ProjectInfo, UserInfo
from .rest.client import RestClient
from .soap.client import SoapClient, as_list


def projects_via_rest(rest: RestClient, limit: int = 2000) -> list[ProjectInfo]:
    out = []
    for item in rest.iterate("/projects", params={"fields[projects]": "id,name,active"},
                             page_size=100, max_items=limit):
        attrs = item.get("attributes") or {}
        out.append(ProjectInfo(id=attrs.get("id") or item.get("id", ""), name=attrs.get("name", ""),
                               active=attrs.get("active")))
    return sorted(out, key=lambda p: p.id.lower())


def projects_via_soap(soap: SoapClient) -> list[ProjectInfo]:
    root = soap.call("Project", "getRootProjectGroup")
    group = _first_value(root)
    uri = group.get("uri") if isinstance(group, dict) else None
    if not uri:
        return []
    resp = soap.call("Project", "getDeepContainedProjects", {"projectGroupURI": uri})
    out = []
    for p in as_list(resp):
        if isinstance(p, dict):
            out.append(ProjectInfo(id=p.get("id", ""), name=p.get("name", "") or ""))
    return sorted((p for p in out if p.id), key=lambda p: p.id.lower())


def current_user_via_rest(rest: RestClient) -> UserInfo:
    data = rest.current_user().get("data") or {}
    attrs = data.get("attributes") or {}
    return UserInfo(id=attrs.get("id") or data.get("id", ""), name=attrs.get("name", ""))


def _first_value(parsed: Any) -> Any:
    if isinstance(parsed, dict) and len(parsed) == 1:
        return next(iter(parsed.values()))
    return parsed
