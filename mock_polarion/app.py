"""WSGI mock of the Polarion surfaces Phase 1 needs.

Behaviour mirrors what was observed on a real Polarion 2606 (2026-11-06):
- /polarion/                 login page with buildId=...-<release>.<n>-<hash> and "Version NNNN"
- /polarion/rest/v1/*        401 JSON "No access token" for every path without a token
- /polarion/rest/v1/definition  OpenAPI served without a token (when Swagger is on)
- /polarion/ws/services/X    WSDL on ?wsdl; SOAP sessionID header returned on every call;
                              failed login -> Fault "Authentication failed."
- /repo/                     401 Basic realm="Subversion repository" without credentials
REST paths per release come from the same endpoint catalog the application uses.
"""
from __future__ import annotations

import base64
import json
import random
import re
import xml.etree.ElementTree as ET
from typing import Callable, Iterable
from urllib.parse import parse_qs, unquote

from pavs.polarion.capabilities.catalog import EndpointCatalog
from pavs.polarion.version.release import Release

from . import config_api as C
from .profiles import MockProfile, MockUser
from .repo_fixture import DOCUMENTS, FILES, PAGES
from .workitems import WorkItems

SOAP_OPS: dict[str, list[str]] = {
    "Session": ["logIn", "logInWithToken", "endSession", "hasSubject", "beginTransaction",
                "endTransaction", "transactionExists"],
    "Security": ["getGlobalRoles", "getContextRoles", "getRolesForUser", "hasPermission",
                 "hasCurrentUserPermission", "canReadInstance", "canModifyInstance",
                 "canDeleteInstance", "canModifyKey", "getUserFromToken", "getLocationForURI"],
    "Project": ["getRootProjectGroup", "getDeepContainedProjects", "getContainedProjects",
                "getProject", "getUsers", "getUser", "getProjectUsers"],
    "Tracker": ["queryWorkItems", "getAllEnumOptionsForId", "getDefinedCustomFieldTypes",
                "getBackLinkedWorkitems", "getAvailableActions", "getUnavailableActions",
                "performWorkflowAction", "getModules", "queryModules", "getModuleByLocation",
                "getModuleWorkItems", "queryBaselines", "getRevisions", "getDocumentSpaces",
                "getWikiSpaces", "getWikiPages", "queryWorkItemUrisLimited", "queryWorkItemsLimited"],
    "TestManagement": ["searchTestRuns"],
}
IMPL_NS = "http://ws.polarion.com/{svc}WebService-impl"
SESSION_NS = "http://ws.polarion.com/session"

StartResponse = Callable[[str, list], None]


class MockPolarion:
    def __init__(self, profile: MockProfile) -> None:
        self.p = profile
        self.release = Release.parse(profile.release)
        catalog = EndpointCatalog.load()
        self.ops = [o for o in catalog.operations() if o.exists_in(self.release)]
        self.op_keys = {o.key for o in self.ops}
        self.sessions: dict[str, MockUser] = {}
        self.requests: list[tuple[str, str]] = []     # (method, path) log for assertions
        self.store = WorkItems()

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: dict, start_response: StartResponse) -> Iterable[bytes]:
        method = environ["REQUEST_METHOD"].upper()
        path = environ.get("PATH_INFO", "/")
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        self.requests.append((method, path))
        status, hdrs, payload = self.route(method, path, query, headers, body)
        start_response(status, hdrs)
        return [payload]

    def route(self, method, path, query, headers, body):
        if path in ("/polarion", "/polarion/"):
            if "polarion_mock_session=ok" in headers.get("cookie", ""):
                return _html(200, "<html><head><title>Polarion</title></head><body><div id='portal'>Polarion Portal</div></body></html>")
            return self._login_page()
        if path == "/polarion/login":
            return self._login_page("Authentication failed." if query.get("error") else "")
        if path == "/polarion/j_security_check" and method == "POST":
            form = parse_qs(body.decode("utf-8", "replace"))
            user, pw = form.get("j_username", [""])[0], form.get("j_password", [""])[0]
            if any(u.id == user and u.password and u.password == pw for u in self.p.users):
                return ("302 Found", [("Location", "/polarion/"), ("Set-Cookie", "polarion_mock_session=ok; Path=/")], b"")
            return ("302 Found", [("Location", "/polarion/login?error=1")], b"")
        if path == "/polarion/sdk/doc/rest/changes.txt":
            return _text(f"Version {self.p.internal}\n--------------\n  ### New Endpoints: 0\n")
        if path.startswith("/polarion/rest/v1"):
            return self._rest(method, path[len("/polarion/rest/v1"):] or "/", query, headers, body)
        if path.startswith("/polarion/ws/services/"):
            return self._soap(method, path, query, headers, body)
        if path.startswith("/repo"):
            return self._svn(method, path, headers)
        return _html(404, "<html><body>Not Found</body></html>")

    # ------------------------------------------------------------------ login page
    def _login_page(self, error: str = ""):
        # form field names (j_username, j_password, csrfToken, form#logInForm) copied from a real 2606 login page
        err = f'<div class="errormessage">{error}</div>' if error else ""
        html = (f'<html><head><title>Login</title><link rel="stylesheet" href="/polarion/ria/css/login.css?buildId={self.p.build_id}"/>'
                f'</head><body><div class="version">Version {self.p.release}</div>{err}'
                '<form method="post" id="logInForm" action="j_security_check">'
                '<input id="j_username" name="j_username" type="text"/><input id="j_password" name="j_password" type="password"/>'
                '<input id="csrfToken" name="csrfToken" type="hidden" value="mock"/>'
                '<button type="submit" id="login-button">Log In</button></form></body></html>')
        return _html(200, html)

    # ------------------------------------------------------------------ REST
    def _rest(self, method, sub, query, headers, body=b""):
        if not self.p.rest_enabled:
            return _html(404, "<html><body>HTTP Status 404 - Not Found</body></html>")
        if sub == "/definition" and method == "GET":
            if not self.p.swagger_enabled:
                return _html(404, "<html><body>Not Found</body></html>")
            paths: dict = {}
            for o in self.ops:
                paths.setdefault(o.path, {})[o.method.lower()] = {"summary": o.summary}
            return _json(200, {"openapi": "3.0.1", "info": {"title": "Polarion REST API", "version": "v1"},
                               "paths": paths})
        user = self._bearer_user(headers)
        if user is None:
            detail = "No access token" if "authorization" not in headers else "Invalid token"
            return _json(401, {"errors": [{"status": "401", "title": "Unauthorized", "detail": detail}]})
        self._current_user = user
        if not user.rest_allowed:
            return _json(403, {"errors": [{"status": "403", "title": "Forbidden", "detail":
                         "Sorry, you do not have the necessary permissions to perform this operation."}]})
        if not self._route_exists(method, sub):
            return _json(404, {"errors": [{"status": "404", "title": "Not Found",
                                           "detail": f"No endpoint {method} {sub}"}]})
        if method in user.denied_verbs:
            return _json(403, {"errors": [{"status": "403", "title": "Forbidden", "detail":
                         "Sorry, you do not have the necessary permissions to perform this operation."}]})
        if method != "GET":
            return self._rest_write(method, sub, query, user, body)
        return self._rest_get(sub, query, user)

    def _rest_write(self, method, sub, query, user, body):
        import json as _json_mod
        try:
            payload = _json_mod.loads(body) if body else None
        except ValueError:
            return _json(400, {"errors": [{"status": "400", "title": "Bad Request", "detail": "invalid JSON"}]})
        m = re.match(r"^/projects/([^/]+)/workitems(?:/([^/]+)(/comments)?)?$", sub)
        if not m:
            return _json(405, {"errors": [{"status": "405", "title": "Method Not Allowed", "detail": "not supported by the mock"}]})
        project, ref, comments = m.groups()
        if project not in self.store.items:
            return _json(404, {"errors": [{"status": "404", "title": "Not Found", "detail": f"Project '{project}' not found."}]})
        if ref is None and method == "POST":
            code, obj = self.store.post(project, payload, user)
        elif ref is None and method == "DELETE":
            code, obj = self.store.delete(project, payload, user)
        elif ref and comments and method == "POST":
            code, obj = self.store.comment(project, ref, payload, user)
        elif ref and not comments and method == "PATCH":
            code, obj = self.store.patch(project, ref, payload, query, user)
        else:
            return _json(405, {"errors": [{"status": "405", "title": "Method Not Allowed", "detail": f"{method} {sub}"}]})
        if obj is None:
            return ("204 No Content", [], b"")
        return _json(code, obj)

    def _route_exists(self, method: str, sub: str) -> bool:
        parts = [p for p in sub.split("/") if p]
        for o in self.ops:
            if o.method != method:
                continue
            tparts = [p for p in o.path.split("/") if p]
            if len(tparts) == len(parts) and all(t.startswith("{") or t == p for t, p in zip(tparts, parts)):
                return True
        return False

    def _rest_get(self, sub, query, user: MockUser):
        if sub == "/user":
            return _json(200, {"data": {"type": "users", "id": user.id,
                                        "attributes": {"id": user.id, "name": user.name}}})
        if sub == "/metadata":
            return _json(200, {"data": {"type": "metadata", "id": "metadata", "attributes": {
                "version": self.p.internal, "build": self.p.build_id, "timezone": "+00:00"}}})
        if sub == "/projects":
            items = [{"type": "projects", "id": pid, "attributes": {"id": pid, "name": name, "active": True}}
                     for pid, name in self.p.projects]
            return _json(200, _page(items, query, "/polarion/rest/v1/projects"))
        if sub == "/users":
            items = [{"type": "users", "id": u.id, "attributes": {"id": u.id, "name": u.name}}
                     for u in self.p.users]
            return _json(200, _page(items, query, "/polarion/rest/v1/users"))
        m = re.match(r"^/projects/([^/]+)", sub)
        if m and m.group(1) not in {p for p, _ in self.p.projects}:
            return _json(404, {"errors": [{"status": "404", "title": "Not Found",
                                           "detail": f"Project '{m.group(1)}' not found."}]})
        if m:
            out = self._rest_project(m.group(1), sub[m.end():], query)
            if out is not None:
                return out
        return _json(200, {"data": [], "links": {"self": "/polarion/rest/v1" + sub}})

    def _rest_project(self, project: str, rest: str, query):
        base = f"/polarion/rest/v1/projects/{project}"
        m = re.match(r"^/enumerations/([^/]+)/([^/]+)/([^/]+)$", rest)
        if m:
            ctx, name, target = (None if v == "~" else v for v in m.groups())
            enum_id = f"{ctx}/{name}" if ctx else name
            opts = C.enum_options(project, enum_id, target)
            if opts is None:
                return _json(404, {"errors": [{"status": "404", "title": "Not Found",
                                               "detail": f"Enumeration {enum_id} not found"}]})
            options = [{"id": o.id, "name": o.name, "default": o.default, "oppositeName": o.opposite_name,
                        "parent": o.parent, "terminal": o.terminal,
                        "linkRules": [{"fromTypes": [a], "toTypes": [b]} for a, b in o.rules]}
                       for o in opts]
            return _json(200, {"data": {"type": "enumerations",
                                        "id": f"{project}/{m.group(1)}/{name}/{m.group(3)}",
                                        "attributes": {"enumName": name, "options": options}}})
        if rest == "/enumerations":
            items = []
            for path in sorted(FILES):
                if path.endswith("-enum.xml") and "/tracker/fields/" in path:
                    name = path.rsplit("/", 1)[-1][: -len("-enum.xml")]
                    opts = C.enum_options(project, name) or []
                    items.append({"type": "enumerations", "id": f"{project}/~/{name}/~", "attributes": {
                        "enumName": name, "options": [{"id": o.id, "name": o.name} for o in opts]}})
            return _json(200, _page(items, query, base + "/enumerations"))
        m = re.match(r"^/customfields/workitems/([^/]+)$", rest)
        if m:
            fields = []
            for f in C.custom_fields(project, m.group(1)):
                if f.enum_id and not f.enum_id.startswith("@"):
                    t = {"kind": "enumeration", "enumName": f.enum_id}
                elif f.enum_id:
                    t = {"kind": "relationship", "targetResourceTypes": ["users"]}
                else:
                    t = {"kind": f.type}
                fields.append({"id": f.id, "name": f.name, "required": f.required, "type": t})
            return _json(200, {"data": {"type": "customfields", "id": f"{project}/workitems/{m.group(1)}",
                                        "attributes": {"resourceType": "workitems", "targetType": m.group(1),
                                                       "fields": fields}}})
        if rest == "/documents":
            items = [{"type": "documents", "id": f"{project}/{sp}/{nm}", "attributes": {
                "moduleFolder": sp, "moduleName": nm, "title": ti, "type": ty, "status": st}}
                for sp, nm, ti, ty, st in (DOCUMENTS if project == "acme_demo" else [])]
            return _json(200, _page(items, query, base + "/documents"))
        if rest == "/pages":
            items = [{"type": "pages", "id": f"{project}/{sp}/{nm}", "attributes": {
                "spaceId": sp, "pageName": nm, "title": ti}}
                for sp, nm, ti in (PAGES if project == "acme_demo" else [])]
            return _json(200, _page(items, query, base + "/pages"))
        if rest == "/workitems":
            return _json(200, _page(self.store.rest_list(project, query), query, base + "/workitems"))
        m = re.match(r"^/workitems/([^/]+)$", rest)
        if m:
            code, obj = self.store.get(project, m.group(1), self._current_user)
            return _json(code, obj)
        m = re.match(r"^/workitems/([^/]+)/actions/getWorkflowActions$", rest)
        if m:
            found = self.store.find(m.group(1))
            hit = (found[1]["type"], found[1]["status"]) if found else None
            if hit is None:
                return _json(404, {"errors": [{"status": "404", "title": "Not Found", "detail": "no such item"}]})
            return _json(200, {"data": self._item_actions(project, *hit)})
        return None

    def _item_actions(self, project: str, wi_type: str, status: str) -> list[dict]:
        wf = C.workflow(project, wi_type)
        if wf is None:
            return []
        out = []
        seen: set[str] = set()
        # real Polarion lists each action once: the transition from the current status if any
        ordered = sorted(wf.transitions, key=lambda tr: tr.source != status)
        for i, t in enumerate(ordered):
            if t.action in seen:
                continue
            seen.add(t.action)
            a = wf.action(t.action)
            ok = t.source == status
            item = {"id": str(i), "nativeActionId": t.action, "name": a.name if a else t.action,
                    "available": ok, "targetStatus": t.target,
                    "requiredFields": a.required_fields if a else []}
            if not ok:
                item["unavailableReason"] = f"Not available from status '{status}'."
            out.append(item)
        return out

    def _bearer_user(self, headers) -> MockUser | None:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[7:].strip()
        return next((u for u in self.p.users if u.token and u.token == token), None)

    # ------------------------------------------------------------------ SOAP
    def _soap(self, method, path, query, headers, body):
        if not self.p.soap_enabled:
            return _html(404, "<html><body>Not Found</body></html>")
        m = re.match(r"^/polarion/ws/services/(\w+)WebService$", path)
        if not m or m.group(1) not in SOAP_OPS:
            return _html(404, "<html><body>Not Found</body></html>")
        svc = m.group(1)
        if method == "GET" and "wsdl" in query:
            return _xml(200, self._wsdl(svc))
        if method != "POST":
            return _html(405, "<html><body>Method Not Allowed</body></html>")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return _xml(500, _fault("soapenv:Client", "Malformed XML"))
        sid = None
        req = None
        for el in root.iter():
            if el.tag == f"{{{SESSION_NS}}}sessionID":
                sid = (el.text or "").strip()
        bodies = [e for e in root if e.tag.endswith("Body")]
        if bodies and len(bodies[0]):
            req = bodies[0][0]
        if req is None:
            return _xml(500, _fault("soapenv:Client", "No operation"))
        op = req.tag.rsplit("}", 1)[-1]
        args = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in req}
        if op not in SOAP_OPS[svc]:
            return _xml(500, _fault("soapenv:Client", f"No such operation '{op}'"))
        sid = sid or str(random.randint(10**17, 10**18))
        user = self.sessions.get(sid)
        return _xml(200, self._soap_op(svc, op, args, sid, user))

    def _soap_op(self, svc, op, args, sid, user):
        ns = IMPL_NS.format(svc=svc)
        if svc == "Session":
            if op == "logIn":
                u = next((x for x in self.p.users if x.id == args.get("userName")
                          and x.password and x.password == args.get("password")), None)
                if u is None:
                    return _fault("soapenv:Server.generalException", _AUTH_FAIL)
                self.sessions[sid] = u
                return _envelope(sid, f'<logInResponse xmlns="{ns}"/>')
            if op == "logInWithToken":
                u = next((x for x in self.p.users if x.token and x.token == args.get("token")), None)
                if u is None or args.get("mechanism") != "AccessToken":
                    return _fault("soapenv:Server.generalException", _AUTH_FAIL)
                self.sessions[sid] = u
                return _envelope(sid, f'<logInWithTokenResponse xmlns="{ns}"/>')
            if op == "hasSubject":
                return _envelope(sid, _ret(ns, op, ["true" if user else "false"]))
            if op == "endSession":
                self.sessions.pop(sid, None)
                return _envelope(sid, f'<endSessionResponse xmlns="{ns}"/>')
            return _envelope(sid, f'<{op}Response xmlns="{ns}"/>')
        if user is None:
            return _fault("soapenv:Server.generalException", "Not authorized.")
        if svc == "Security" and op == "getUserFromToken":
            u = next((x for x in self.p.users if x.token == args.get("token")), None)
            return _envelope(sid, _ret(ns, op, [u.id if u else ""]))
        if svc == "Security" and op == "getGlobalRoles":
            roles = sorted({r for u in self.p.users for r in u.global_roles})
            return _envelope(sid, _ret(ns, op, roles))
        if svc == "Project" and op == "getRootProjectGroup":
            return _envelope(sid, f'<{op}Response xmlns="{ns}"><{op}Return '
                                  f'uri="subterra:data-service:objects:/default/${{ProjectGroup}}" '
                                  f'unresolvable="false"><name>Root</name></{op}Return></{op}Response>')
        if svc == "Project" and op == "getDeepContainedProjects":
            items = "".join(
                f'<{op}Return uri="subterra:data-service:objects:/default/{pid}${{Project}}{pid}" '
                f'unresolvable="false"><id>{pid}</id><name>{name}</name></{op}Return>'
                for pid, name in self.p.projects)
            return _envelope(sid, f'<{op}Response xmlns="{ns}">{items}</{op}Response>')
        try:
            out = self._soap_config(op, args)
        except _SoapUserFault as exc:
            return _fault("soapenv:Server.userException", str(exc))
        if out is not None:
            return _envelope(sid, f'<{op}Response xmlns="{ns}">{out}</{op}Response>')
        return _envelope(sid, f'<{op}Response xmlns="{ns}"/>')

    def _soap_config(self, op: str, args: dict) -> str | None:
        R = f"{op}Return"
        project = args.get("projectID") or args.get("projectId")
        if op == "getProject":
            pid = args.get("projectID", "")
            return (f'<{R} uri="subterra:data-service:objects:/default/{pid}${{Project}}{pid}" '
                    f'unresolvable="false"><id>{pid}</id><location>default:/{pid}/.polarion/polarion-project.xml</location></{R}>')
        if op in ("getContextRoles", "getRolesForUser") and "/.polarion/" in (args.get("location") or "x"):
            # real 2606: only the project FOLDER is a valid context, not the descriptor file Project.location returns
            raise _SoapUserFault("com.polarion.alm.ws.WebserviceException: Location is not a valid context (Project, ProjectGroup)")
        if op == "getContextRoles":
            return "".join(f"<{R}>{r}</{R}>" for r in C.project_roles())
        if op == "getProjectUsers":
            return "".join(f'<{R} uri="u"><id>{u.id}</id><name>{u.name}</name></{R}>' for u in self.p.users)
        if op == "getRolesForUser":
            pid = args.get("location", "").split(":/", 1)[-1] or None
            return "".join(f"<{R}>{r}</{R}>" for r in C.roles_for_user(pid, args.get("userId", "")))
        if op == "hasPermission":
            ok = C.has_permission(args.get("projectId"), args.get("userId", ""), args.get("permission", ""))
            return f"<{R}>{'true' if ok else 'false'}</{R}>"
        if op == "getAllEnumOptionsForId":
            opts = C.enum_options(project or "", args.get("enumID", "")) or []
            return "".join(
                f"<{R}><default>{'true' if o.default else 'false'}</default><enumId>{args.get('enumID')}</enumId>"
                f"<hidden>false</hidden><id>{o.id}</id><name>{o.name}</name>"
                f"<sequenceNumber>{i}</sequenceNumber></{R}>" for i, o in enumerate(opts))
        if op == "getDefinedCustomFieldTypes":
            parts = []
            for f in C.custom_fields(project or "", args.get("typeID", "")):
                enum = f"<enumId>{f.enum_id}</enumId>" if f.enum_id else ""
                parts.append(f"<{R}><description></description><id>{f.id}</id>"
                             f"<multi>{str(f.multi).lower()}</multi><name>{f.name}</name>"
                             f"<required>{str(f.required).lower()}</required>"
                             f"<type>xsd:{f.type}</type>{enum}</{R}>")
            return "".join(parts)
        if op == "queryModules":
            pid = args.get("query", "").replace("project.id:", "").strip()
            return "".join(
                f'<{R} uri="subterra:data-service:objects:/default/{pid}${{Module}}{sp}/{nm}" unresolvable="false">'
                f"<moduleFolder>{sp}</moduleFolder><moduleName>{nm}</moduleName><status><id>{st}</id></status>"
                f"<title>{ti}</title><type><id>{ty}</id></type></{R}>"
                for sp, nm, ti, ty, st in (DOCUMENTS if pid == "acme_demo" else []))
        if op == "getWikiSpaces":
            if project != "acme_demo":
                return ""
            return "".join(f"<{R}>{sp}</{R}>" for sp in sorted({pg[0] for pg in PAGES}))
        if op == "getWikiPages":
            sp = args.get("spaceId")
            return "".join(f'<{R} uri="w"><pageName>{nm}</pageName><spaceId>{s2}</spaceId><title>{ti}</title></{R}>'
                           for s2, nm, ti in PAGES if s2 == sp and project == "acme_demo")
        if op == "queryWorkItemUrisLimited":
            q = args.get("query", "")
            pid = q.split("project.id:", 1)[1].split()[0] if "project.id:" in q else ""
            t = q.split("type:", 1)[1].split()[0] if "type:" in q else None
            first = self.store.first_per_type(pid)
            return "".join(f"<{R}>subterra:data-service:objects:/default/{pid}${{WorkItem}}{it['id']}</{R}>"
                           for ty, it in first.items() if ty == t)
        if op == "queryWorkItemsLimited":
            pid = args.get("query", "").split("project.id:", 1)[-1].split()[0] if "project.id:" in args.get("query", "") else ""
            parts = []
            for it in sorted(self.store.items.get(pid, {}).values(), key=lambda i: i["id"]):
                links = "".join(
                    f"<LinkedWorkItem><role><id>{role}</id></role><suspect>false</suspect>"
                    f"<workItemURI>subterra:data-service:objects:/default/{pid}${{WorkItem}}{tgt}</workItemURI></LinkedWorkItem>"
                    for role, tgt in it["links"])
                parts.append(f'<{R} uri="subterra:data-service:objects:/default/{pid}${{WorkItem}}{it["id"]}" unresolvable="false">'
                             f"<id>{it['id']}</id><type><id>{it['type']}</id></type><status><id>{it['status']}</id></status>"
                             f"<linkedWorkItems>{links}</linkedWorkItems></{R}>")
            return "".join(parts)
        if op in ("getAvailableActions", "getUnavailableActions"):
            wid = args.get("workitemURI", "").rsplit("}", 1)[-1]
            found = self.store.find(wid)
            hit = (found[1]["type"], found[1]["status"]) if found else None
            if hit is None:
                return ""
            want = op == "getAvailableActions"
            parts = []
            for a in self._item_actions("acme_demo", *hit):
                if a["available"] != want:
                    continue
                req = "".join(f"<requiredFeatures>{x}</requiredFeatures>" for x in a["requiredFields"])
                msg = "" if want else f"<unavailabilityMessage>{a.get('unavailableReason', '')}</unavailabilityMessage>"
                parts.append(f"<{R}><actionId>{a['id']}</actionId><actionName>{a['name']}</actionName>"
                             f"<nativeActionId>{a['nativeActionId']}</nativeActionId>{req}"
                             f"<targetStatus><id>{a['targetStatus']}</id></targetStatus>{msg}</{R}>")
            return "".join(parts)
        return None

    def _wsdl(self, svc: str) -> str:
        ops = "".join(f'<wsdl:operation name="{o}"/>' for o in SOAP_OPS[svc])
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" '
                f'targetNamespace="http://ws.polarion.com/{svc}WebService">'
                f'<wsdl:portType name="{svc}WebService">{ops}</wsdl:portType></wsdl:definitions>')

    # ------------------------------------------------------------------ SVN
    def _svn(self, method, path, headers):
        if not self.p.svn_enabled:
            return _html(404, "<html><body>Not Found</body></html>")
        auth = headers.get("authorization", "")
        user = None
        if auth.lower().startswith("basic "):
            try:
                uid, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                user = next((u for u in self.p.users if u.id == uid and u.password == pw), None)
            except Exception:
                user = None
        if user is None:
            return ("401 Unauthorized", [("Content-Type", "text/html"),
                    ("WWW-Authenticate", 'Basic realm="Subversion repository"')], b"Unauthorized")
        if method not in ("GET", "PROPFIND"):
            return ("405 Method Not Allowed", [("Content-Type", "text/plain")], b"read-only mock")
        rel = unquote(path[len("/repo"):]).strip("/")
        if rel in FILES and method == "GET":
            return _xml(200, FILES[rel])
        prefix = (rel + "/") if rel else ""
        children = sorted({p[len(prefix):].split("/", 1)[0] + ("/" if "/" in p[len(prefix):] else "")
                           for p in FILES if p.startswith(prefix)})
        if not children:
            return _html(404, "<html><body>Not Found</body></html>")
        if method == "PROPFIND":
            hrefs = [f"/repo/{prefix}"] + [f"/repo/{prefix}{c}" for c in children]
            body = "".join(f"<D:response><D:href>{h}</D:href></D:response>" for h in hrefs)
            return ("207 Multi-Status", [("Content-Type", "text/xml")],
                    f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">{body}</D:multistatus>'.encode())
        items = "".join(f'<li><a href="{c}">{c}</a></li>' for c in children)
        return _html(200, f"<html><body><h2>Revision 1: /{rel}</h2><ul>{items}</ul>"
                          "<em>Powered by svn</em></body></html>")


# ---------------------------------------------------------------------- helpers
_AUTH_FAIL = ("Authentication failed.; nested exception is: \n\tcom.polarion.platform.security."
              "AuthenticationFailedException: Your Username or Password is incorrect.")


def _page(items: list, query: dict, base: str) -> dict:
    size = int((query.get("page[size]") or ["100"])[0] or 100)
    number = int((query.get("page[number]") or ["1"])[0] or 1)
    start = (number - 1) * size
    chunk = items[start:start + size]
    out: dict = {"data": chunk, "links": {"self": f"{base}?page%5Bsize%5D={size}&page%5Bnumber%5D={number}"}}
    if start + size < len(items):
        out["links"]["next"] = f"{base}?page%5Bsize%5D={size}&page%5Bnumber%5D={number + 1}"
        out["meta"] = {"totalCount": len(items)}
    return out


def _ret(ns: str, op: str, values: list[str]) -> str:
    inner = "".join(f"<{op}Return>{v}</{op}Return>" for v in values)
    return f'<{op}Response xmlns="{ns}">{inner}</{op}Response>'


def _envelope(sid: str, body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soapenv:Header><ns1:sessionID xmlns:ns1="{SESSION_NS}">{sid}</ns1:sessionID></soapenv:Header>'
            f'<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>')


class _SoapUserFault(Exception):
    """A SOAP user exception like the ones the real server raises for a bad argument."""


def _fault(code: str, msg: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
            f'<soapenv:Fault><faultcode>{code}</faultcode><faultstring>{msg}</faultstring></soapenv:Fault>'
            '</soapenv:Body></soapenv:Envelope>')


def _json(code: int, obj) -> tuple:
    return (f"{code} {_REASON.get(code, '')}", [("Content-Type", "application/json")],
            json.dumps(obj).encode())


def _html(code: int, html: str) -> tuple:
    return (f"{code} {_REASON.get(code, '')}", [("Content-Type", "text/html;charset=UTF-8")], html.encode())


def _text(text: str) -> tuple:
    return ("200 OK", [("Content-Type", "text/plain")], text.encode())


def _xml(code: int, xml: str) -> tuple:
    return (f"{code} {_REASON.get(code, '')}", [("Content-Type", "text/xml;charset=utf-8")], xml.encode())


_REASON = {200: "OK", 302: "Found", 207: "Multi-Status", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 500: "Internal Server Error"}
