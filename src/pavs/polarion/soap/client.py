"""Polarion SOAP Web Services client.

Document/literal envelopes over the shared HttpTransport, so TLS, timeouts, redaction and
evidence recording match the REST provider. Shapes verified against Polarion 2606:
- endpoint  <base>/ws/services/<Service>WebService
- namespace http://ws.polarion.com/<Service>WebService-impl
- session   header {http://ws.polarion.com/session}sessionID (returned by the server)
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable
from xml.sax.saxutils import escape

from ...infra import safexml
from ...domain.errors import AuthenticationFailed, PavsError, PolarionApiError, SoapFault
from ..safety import WriteAuthorization
from ..transport import HttpTransport

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SESSION_NS = "http://ws.polarion.com/session"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
TYPES_NS = "http://ws.polarion.com/types"

# Operations that only read. Anything else counts as a write and goes through the Write Guard.
READ_OPERATION_PREFIXES = ("get", "query", "has", "can", "is", "find", "search", "count")
SESSION_READ_OPS = frozenset({"logIn", "logInWithToken", "endSession", "hasSubject",
                              "transactionExists", "beginTransaction", "endTransaction"})


def is_read_operation(service: str, operation: str) -> bool:
    if service == "Session":
        return operation in SESSION_READ_OPS
    return operation.startswith(READ_OPERATION_PREFIXES)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_to_python(elem: ET.Element) -> Any:
    """Children grouped by local name; repeated names become lists; leaves become text.

    xsi:nil elements become None. "true"/"false" leaves are kept as strings: callers convert.
    """
    if elem.attrib.get(f"{{{XSI}}}nil") == "true":
        return None
    # Polarion types carry identity as attributes, e.g. <...Return uri="subterra:..." unresolvable="false">
    attrs = {_local(k): v for k, v in elem.attrib.items() if not k.startswith(f"{{{XSI}}}")}
    children = list(elem)
    if not children:
        text = elem.text if elem.text is not None else ""
        if attrs:
            return {**attrs, "_text": text} if text.strip() else attrs
        return text
    out: dict[str, Any] = dict(attrs)
    repeated: set[str] = set()
    for child in children:
        name = _local(child.tag)
        value = element_to_python(child)
        if name in out:
            if name not in repeated:
                out[name] = [out[name]]
                repeated.add(name)
            out[name].append(value)
        else:
            out[name] = value
    return out


def _serialize(name: str, value: Any) -> str:
    if value is None:
        return f'<{name} xsi:nil="true"/>'
    if isinstance(value, bool):
        return f"<{name}>{'true' if value else 'false'}</{name}>"
    if isinstance(value, (list, tuple)):
        return "".join(_serialize(name, v) for v in value)
    if isinstance(value, dict):
        # keys starting with "@" are XML attributes (for example "@uri" on an object reference); the others are child elements
        attrs = "".join(f' {k[1:]}="{escape(str(v), {chr(34): "&quot;"})}"' for k, v in value.items() if k.startswith("@"))
        inner = "".join(_serialize(k, v) for k, v in value.items() if not k.startswith("@"))
        return f"<{name}{attrs}>{inner}</{name}>"
    return f"<{name}>{escape(str(value))}</{name}>"


class SoapClient:
    def __init__(self, transport: HttpTransport, base_url: str) -> None:
        self.transport = transport
        self.base = base_url.rstrip("/") + "/ws/services"
        self.session_id: str | None = None
        self.logged_in_user: str | None = None
        self._reauth = None                       # set by the connection manager: signs in again with the same credentials

    def endpoint(self, service: str) -> str:
        return f"{self.base}/{service}WebService"

    @staticmethod
    def namespace(service: str) -> str:
        return f"http://ws.polarion.com/{service}WebService-impl"

    @staticmethod
    def types_namespace(service: str) -> str:
        """Namespace of the service's data types (the children of a Work Item, Module, Comment ... are qualified with it)."""
        return f"http://ws.polarion.com/{service}WebService-types"

    # ------------------------------------------------------------------ discovery
    def wsdl_operations(self, service: str) -> set[str] | None:
        """Operation names from the live WSDL, or None if the service is not served."""
        resp = self.transport.request("GET", self.endpoint(service), provider="soap",
                                      params={"wsdl": ""})
        if resp.status_code != 200 or b"definitions" not in resp.content[:2000]:
            return None
        try:
            root = safexml.fromstring(resp.content)
        except ET.ParseError:
            return None
        ops = {op.attrib["name"] for op in root.iter()
               if _local(op.tag) == "operation" and "name" in op.attrib}
        return ops

    # ------------------------------------------------------------------ calls
    def build_envelope(self, service: str, operation: str, params: dict[str, Any] | Iterable) -> str:
        items = params.items() if isinstance(params, dict) else params
        body = "".join(_serialize(f"ns:{k}", v) for k, v in items)
        header = ""
        if self.session_id:
            header = f'<s:sessionID xmlns:s="{SESSION_NS}">{escape(self.session_id)}</s:sessionID>'
        return (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:xsi="{XSI}" '
                f'xmlns:ns="{self.namespace(service)}" xmlns:t="{self.types_namespace(service)}" xmlns:ty="{TYPES_NS}">'
                f"<soapenv:Header>{header}</soapenv:Header>"
                f"<soapenv:Body><ns:{operation}>{body}</ns:{operation}></soapenv:Body>"
                f"</soapenv:Envelope>")

    def relogin(self) -> None:
        """Sign in again with the stored credentials (raises PavsError when none are known)."""
        if self._reauth is None:
            raise PavsError("no re-login available")
        self._reauth()

    def set_reauth(self, fn) -> None:
        """`fn()` signs in again (token or password). Used when the server forgot the session (timeout)."""
        self._reauth = fn

    def call(self, service: str, operation: str, params: dict[str, Any] | None = None, *,
             write_auth: WriteAuthorization | None = None, project_id: str | None = None) -> Any:
        """Invoke an operation and return the parsed content of `<operation>Response`.

        A real Polarion answers a call on an expired session with the fault "Not authorized." (verified on 2606). When that
        happens, and the server confirms the session is gone (`hasSubject` is false), the client signs in again and repeats the
        call ONCE, for read operations only: a write is never repeated automatically."""
        try:
            return self._call_once(service, operation, params, write_auth=write_auth, project_id=project_id)
        except SoapFault as exc:
            if not self._can_recover(service, operation, exc):
                raise
        self._reauth()
        return self._call_once(service, operation, params, write_auth=write_auth, project_id=project_id)

    def _can_recover(self, service: str, operation: str, exc: SoapFault) -> bool:
        if self._reauth is None or service == "Session" or "not authorized" not in exc.message.lower():
            return False
        if not is_read_operation(service, operation):
            return False
        try:
            return _scalar(self._call_once("Session", "hasSubject", {})) != "true"     # False: a genuine permission denial
        except PavsError:
            return False

    def _call_once(self, service: str, operation: str, params: dict[str, Any] | None = None, *,
                   write_auth: WriteAuthorization | None = None, project_id: str | None = None) -> Any:
        envelope = self.build_envelope(service, operation, params or {})
        resp = self.transport.request(
            "POST", self.endpoint(service), provider="soap",
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            content=envelope.encode("utf-8"), write_auth=write_auth,
            is_write=not is_read_operation(service, operation), project_id=project_id,
            retry=is_read_operation(service, operation))
        if resp.status_code == 404:
            raise PolarionApiError(404, "Not Found", f"SOAP service {service}WebService is not served")
        try:
            root = safexml.fromstring(resp.content)
        except ET.ParseError:
            raise PolarionApiError(resp.status_code, "Invalid SOAP response",
                                   resp.text[:200]) from None
        self._capture_session(root)
        body = next((e for e in root if _local(e.tag) == "Body"), None)
        if body is None or not len(body):
            raise PolarionApiError(resp.status_code, "Invalid SOAP response", "no Body")
        payload = body[0]
        if _local(payload.tag) == "Fault":
            code = (payload.findtext("faultcode") or "").strip()
            msg = (payload.findtext("faultstring") or "").strip()
            if re.search(r"authentication failed|AuthenticationFailedException", msg, re.I):
                raise AuthenticationFailed(401, "Authentication failed", _first_line(msg))
            raise SoapFault(code, _first_line(msg))
        return element_to_python(payload)

    def _capture_session(self, root: ET.Element) -> None:
        header = next((e for e in root if _local(e.tag) == "Header"), None)
        if header is None:
            return
        for e in header:
            if e.tag == f"{{{SESSION_NS}}}sessionID" and e.text:
                self.session_id = e.text.strip()

    # ------------------------------------------------------------------ session
    def login_password(self, username: str, password: str) -> None:
        self.call("Session", "logIn", {"userName": username, "password": password})
        self.logged_in_user = username

    def login_token(self, token: str) -> None:
        self.call("Session", "logInWithToken",
                  {"mechanism": "AccessToken", "username": "", "token": token})
        try:
            user = self.call("Security", "getUserFromToken", {"token": token})
            self.logged_in_user = _scalar(user) or None
        except Exception:
            self.logged_in_user = None

    def has_subject(self) -> bool:
        return _scalar(self.call("Session", "hasSubject")) == "true"

    def logout(self) -> None:
        if self.session_id:
            try:
                self.call("Session", "endSession")
            finally:
                self.session_id = None
                self.logged_in_user = None


def _scalar(value: Any) -> str:
    """First leaf text of a parsed response such as {'hasSubjectReturn': 'true'}."""
    while isinstance(value, dict):
        if not value:
            return ""
        value = next(iter(value.values()))
    if isinstance(value, list):
        return _scalar(value[0]) if value else ""
    return value or ""


def as_list(value: Any) -> list:
    """Normalise a parsed array response like {'getGlobalRolesReturn': [...]} to a list."""
    if isinstance(value, dict):
        if len(value) == 1:
            value = next(iter(value.values()))
        else:
            return [value]
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _first_line(msg: str) -> str:
    return msg.split(";")[0].split("\n")[0].strip()
