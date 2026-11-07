"""Effective configuration the mock's REST and SOAP endpoints serve, derived from repo_fixture.

Precedence mirrors Polarion's documented rules, so the API answers and the SVN files agree.
"""
from __future__ import annotations

from pavs.domain.config import Scope, Source
from pavs.domain.enums import ProviderKind
from pavs.engines.inspection import parsers as P

from .repo_fixture import FILES, project_root

_SRC = Source(provider=ProviderKind.SVN, location="mock", scope=Scope.GLOBAL)
_CTX_DIR = {None: "tracker/fields", "documents": "documents/fields"}


def _file(scope: str, project: str | None, rel: str) -> str | None:
    base = ".polarion" if scope == "global" else project_root(project or "")
    return FILES.get(f"{base}/{rel}") if base else None


def enum_options(project: str, enum_id: str, target: str | None = None) -> list | None:
    context, name = (enum_id.split("/", 1) if "/" in enum_id else (None, enum_id))
    d = _CTX_DIR.get(context, f"{context}/fields")
    for scope in ("project", "global"):
        for rel in ([f"{d}/{target}-{name}-enum.xml"] if target else []) + [f"{d}/{name}-enum.xml"]:
            text = _file(scope, project, rel)
            if text:
                return P.parse_enum_options(text, rel)
    return None


def custom_fields(project: str, wi_type: str) -> list:
    merged: dict[str, object] = {}
    for scope, rel in (("global", "tracker/fields/custom-fields.xml"),
                       ("global", f"tracker/fields/{wi_type}-custom-fields.xml"),
                       ("project", "tracker/fields/custom-fields.xml"),
                       ("project", f"tracker/fields/{wi_type}-custom-fields.xml")):
        text = _file(scope, project, rel)
        if text:
            for f in P.parse_custom_fields(text, wi_type, Scope.EFFECTIVE, _SRC):
                merged[f.id] = f
    return list(merged.values())


def workflow(project: str, wi_type: str):
    for scope, rel in (("project", f"tracker/workflow/{wi_type}-workflow.xml"),
                       ("global", f"tracker/workflow/{wi_type}-workflow.xml"),
                       ("project", "tracker/workflow/workflow.xml"), ("global", "tracker/workflow/workflow.xml")):
        text = _file(scope, project, rel)
        if text:
            return P.parse_workflow(text, wi_type, Scope.EFFECTIVE, _SRC)
    return None


def roles_for_user(project: str | None, user: str) -> list[str]:
    roles = []
    for scope in ("global", "project"):
        text = _file(scope, project, "security/user-roles.xml")
        if text:
            roles += [a.role for a in P.parse_user_roles(text, Scope.GLOBAL, _SRC) if a.principal == user]
    return roles


def project_roles() -> list[str]:
    return [r.id for r in P.parse_role_list(FILES[".polarion/security/project-roles.xml"], Scope.GLOBAL, _SRC)]


def has_permission(project: str | None, user: str, permission: str) -> bool:
    roles = set(roles_for_user(project, user))
    if "admin" in roles:
        return True
    rules = []
    for scope in ("global", "project"):
        text = _file(scope, project, "security/permissions.xml")
        if text:
            rules += P.parse_permissions(text, Scope.GLOBAL, _SRC)[0]
    hits = [r for r in rules if r.role in roles and r.permission == permission]
    if any(r.effect == "deny" for r in hits):
        return False
    return any(r.effect == "grant" for r in hits)
