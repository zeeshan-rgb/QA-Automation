"""Roles, role assignments and permissions."""
from __future__ import annotations

from ...domain.config import PermissionCheck, RoleAssignment, RoleDef, Scope
from ...domain.enums import ProviderKind
from ...domain.errors import PavsError
from ...polarion.soap.client import as_list
from .base import REMEDY_SOAP, REMEDY_SVN, Collector, InspectionContext, first_text, src_soap
from .parsers import parse_permissions, parse_role_list, parse_user_roles

MAX_SOAP_USERS = 200


def project_location(ctx: InspectionContext, col: Collector) -> str | None:
    """Repository location of the project, as SOAP Security expects it (from Project.location)."""
    if "project_location" in ctx.shared:
        return ctx.shared["project_location"]
    loc = None
    if ctx.soap is not None:
        proj = col.attempt("SOAP getProject", lambda: ctx.soap.call("Project", "getProject",
                                                                    {"projectID": ctx.project_id}))
        if isinstance(proj, dict):
            body = proj.get("getProjectReturn", proj)
            loc = first_text(body.get("location")) if isinstance(body, dict) else None
    loc = context_location(loc)
    ctx.shared["project_location"] = loc or None
    return loc or None


def context_location(raw: str | None) -> str | None:
    """Project.location is the project's DESCRIPTOR FILE, e.g. 'default:/Vision/.polarion/polarion-project.xml' (verified on a real
    Polarion 2606). Security.getContextRoles / getRolesForUser want the project FOLDER, 'default:/Vision': passing the file is
    refused with 'Location is not a valid context'."""
    if not raw:
        return None
    return raw.split("/.polarion/", 1)[0].rstrip("/") or None


class RolesReader:
    area = "roles"
    title = "Roles & assignments"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        roles: list[RoleDef] = []
        assignments: list[RoleAssignment] = []

        # ---- global roles
        got = False
        if ctx.soap is not None:
            resp = col.attempt("SOAP getGlobalRoles", lambda: ctx.soap.call("Security", "getGlobalRoles"))
            if resp is not None:
                roles += [RoleDef(id=r, scope=Scope.GLOBAL, sources=[src_soap("Security.getGlobalRoles")])
                          for r in as_list(resp) if isinstance(r, str) and r]
                col.used(ProviderKind.SOAP)
                got = True
        if not got and ctx.repo is not None:
            f = col.attempt("SVN roles.xml", lambda: ctx.repo.file(Scope.GLOBAL, None, "security/roles.xml"))
            if f:
                roles += parse_role_list(f[0], Scope.GLOBAL, f[1])
                col.used(ProviderKind.SVN)
                got = True
        col.ok() if got else col.gap("Global roles", "neither SOAP nor the SVN reader is available", REMEDY_SOAP)

        # ---- project roles
        got = False
        loc = project_location(ctx, col) if ctx.soap is not None else None
        if ctx.soap is not None and loc:
            resp = col.attempt("SOAP getContextRoles", lambda: ctx.soap.call(
                "Security", "getContextRoles", {"location": loc}))
            if resp is not None:
                roles += [RoleDef(id=r, scope=Scope.PROJECT, sources=[src_soap("Security.getContextRoles")])
                          for r in as_list(resp) if isinstance(r, str) and r]
                got = True
        if not got and ctx.repo is not None:
            f = col.attempt("SVN project-roles.xml",
                            lambda: ctx.repo.file(Scope.GLOBAL, None, "security/project-roles.xml"))
            if f:
                roles += parse_role_list(f[0], Scope.PROJECT, f[1])
                col.used(ProviderKind.SVN)
                got = True
            # roles defined by the project itself (<project>/.polarion/security/project-roles.xml) add to the global ones
            own = col.attempt("SVN project-level project-roles.xml",
                              lambda: ctx.repo.file(Scope.PROJECT, ctx.project_id, "security/project-roles.xml"))
            if own:
                have = {r.id for r in roles if r.scope == Scope.PROJECT}
                roles += [r for r in parse_role_list(own[0], Scope.PROJECT, own[1]) if r.id not in have]
                col.used(ProviderKind.SVN)
                got = True
        col.ok() if got else col.gap("Project roles", "SOAP project location or SVN reader not available",
                                     REMEDY_SOAP)

        # ---- assignments: SVN gives the complete picture (users and groups, both scopes)
        if ctx.repo is not None:
            for scope in (Scope.GLOBAL, Scope.PROJECT):
                f = col.attempt(f"SVN {scope.value} user-roles.xml",
                                lambda s=scope: ctx.repo.file(s, ctx.project_id, "security/user-roles.xml"))
                if f:
                    assignments += parse_user_roles(f[0], scope, f[1])
            col.used(ProviderKind.SVN)
            col.ok()
        elif ctx.soap is not None and loc:
            assignments += self._assignments_via_soap(ctx, col, loc)
            col.note("Assignments via SOAP list effective roles per project user; group membership is not shown.")
        else:
            col.gap("Role assignments", "needs SOAP (with project location) or the SVN reader", REMEDY_SVN)

        col.result.put("roles", roles)
        col.result.put("assignments", assignments)
        ctx.shared["roles"] = sorted({r.id for r in roles})
        return col.finish()

    def _assignments_via_soap(self, ctx: InspectionContext, col: Collector, loc: str) -> list[RoleAssignment]:
        users = col.attempt("SOAP getProjectUsers", lambda: ctx.soap.call(
            "Project", "getProjectUsers", {"projectID": ctx.project_id}))
        if users is None:
            col.gap("Role assignments", "could not list project users")
            return []
        ids = [u.get("id") for u in as_list(users) if isinstance(u, dict) and u.get("id")]
        if len(ids) > MAX_SOAP_USERS:
            col.note(f"{len(ids)} project users; only the first {MAX_SOAP_USERS} were checked.")
            ids = ids[:MAX_SOAP_USERS]
        out = []
        for uid in ids:
            resp = col.attempt(f"SOAP getRolesForUser({uid})", lambda u=uid: ctx.soap.call(
                "Security", "getRolesForUser", {"userId": u, "location": loc}))
            for role in as_list(resp) if resp is not None else []:
                if isinstance(role, str) and role:
                    out.append(RoleAssignment(principal=uid, role=role, scope=Scope.EFFECTIVE,
                                              sources=[src_soap("Security.getRolesForUser")]))
        col.used(ProviderKind.SOAP)
        col.ok()
        return out


class PermissionsReader:
    area = "permissions"
    title = "Permissions"

    def read(self, ctx: InspectionContext):
        col = Collector(self.area, self.title)
        if ctx.repo is None:
            col.gap("Permission configuration (permissions.xml)",
                    "not exposed by REST or SOAP; only the SVN reader can read it", REMEDY_SVN)
            if ctx.soap is not None:
                col.note("Effective permission checks per user are still possible with SOAP "
                         "(Security.hasPermission) - use 'Check a permission'.")
            return col.finish()
        rules, sets = [], []
        for scope in (Scope.GLOBAL, Scope.PROJECT):
            f = col.attempt(f"SVN {scope.value} permissions.xml",
                            lambda s=scope: ctx.repo.file(s, ctx.project_id, "security/permissions.xml"))
            if f:
                r, c = parse_permissions(f[0], scope, f[1])
                rules += r
                sets += c
        col.used(ProviderKind.SVN)
        col.ok()
        col.result.put("rules", rules)
        col.result.put("customsets", sets)
        col.note("Rules are shown per scope as configured. Project rules add to (or deny) global ones; "
                 "the server's effective answer for a user comes from 'Check a permission' (SOAP).")
        return col.finish()


def check_permission(ctx: InspectionContext, user: str, permission: str) -> PermissionCheck:
    """Ask the server (SOAP Security.hasPermission). Read-only."""
    if ctx.soap is None:
        return PermissionCheck(user=user, permission=permission, project=ctx.project_id, granted=None,
                               detail="SOAP is not signed in for this environment.")
    try:
        resp = ctx.soap.call("Security", "hasPermission",
                             {"userId": user, "permission": permission, "projectId": ctx.project_id})
    except PavsError as exc:
        return PermissionCheck(user=user, permission=permission, project=ctx.project_id, granted=None,
                               detail=str(exc))
    value = first_text(resp)
    return PermissionCheck(user=user, permission=permission, project=ctx.project_id,
                           granted=value == "true", detail=f"server answered {value!r}",
                           source=src_soap("Security.hasPermission"))
