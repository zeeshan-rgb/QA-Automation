import httpx
import pytest

from pavs.domain.errors import AuthenticationFailed, PermissionDenied, SoapFault, TransportError
from pavs.domain.models import TlsSettings
from pavs.polarion.rest.client import RestClient
from pavs.polarion.safety import WriteGuard
from pavs.polarion.soap.client import SoapClient, as_list, element_to_python, is_read_operation
from pavs.polarion.transport import HttpTransport

from .conftest import MOCK_URL, make_env


def _transport(handler=None, wsgi=None, sleeps=None):
    tr = wsgi if wsgi is not None else httpx.MockTransport(handler)
    return HttpTransport(TlsSettings(), 5, WriteGuard(make_env()), transport=tr,
                         sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_retries_on_503_for_reads_only():
    calls, sleeps = [], []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={})
    t = _transport(handler, sleeps=sleeps)
    assert t.request("GET", "http://x/a", provider="t").status_code == 200
    assert len(calls) == 3 and sleeps == [0.5, 1.0]


def test_connect_error_becomes_transport_error():
    def handler(req):
        raise httpx.ConnectError("connection refused")
    with pytest.raises(TransportError, match="Cannot connect"):
        _transport(handler).request("GET", "http://x/a", provider="t")


def test_rest_paging_follows_next(mock_server):
    _, wsgi = mock_server("2606")
    rest = RestClient(_transport(wsgi=wsgi), MOCK_URL, "test-token-admin")
    items = list(rest.iterate("/projects", page_size=1))
    assert [i["id"] for i in items] == ["acme_demo", "acme_test"]


def test_rest_errors_are_typed(mock_server):
    _, wsgi = mock_server("2606")
    with pytest.raises(AuthenticationFailed):
        RestClient(_transport(wsgi=wsgi), MOCK_URL, "wrong-token").get("/user")
    with pytest.raises(PermissionDenied):
        RestClient(_transport(wsgi=wsgi), MOCK_URL, "test-token-reviewer").get("/user")


def test_soap_login_session_and_fault(mock_server):
    app, wsgi = mock_server("2410-rest-off")
    soap = SoapClient(_transport(wsgi=wsgi), MOCK_URL)
    assert soap.has_subject() is False
    assert soap.session_id                                # header captured even anonymously
    with pytest.raises(AuthenticationFailed):
        soap.login_password("admin", "wrong")
    soap.login_password("admin", "admin-test-pw")
    assert soap.has_subject() is True
    roles = as_list(soap.call("Security", "getGlobalRoles"))
    assert "admin" in roles
    with pytest.raises(SoapFault):
        soap.call("Security", "getNoSuchThing")          # read-named, unknown -> server fault
    soap.logout()
    assert soap.session_id is None


def test_soap_token_login_finds_user(mock_server):
    _, wsgi = mock_server("2606")
    soap = SoapClient(_transport(wsgi=wsgi), MOCK_URL)
    soap.login_token("test-token-admin")
    assert soap.logged_in_user == "admin"


def test_soap_write_operations_are_guarded(mock_server):
    from pavs.domain.errors import WriteNotAuthorized
    _, wsgi = mock_server("2606")
    soap = SoapClient(_transport(wsgi=wsgi), MOCK_URL)
    assert is_read_operation("Tracker", "queryWorkItems")
    assert not is_read_operation("Tracker", "performWorkflowAction")
    with pytest.raises(WriteNotAuthorized):
        soap.call("Tracker", "performWorkflowAction", {"workitemURI": "x", "actionId": 1})


def test_element_to_python_handles_attrs_lists_nil():
    import xml.etree.ElementTree as ET
    xml = ('<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
           '<p uri="u1"><id>a</id></p><p uri="u2"><id>b</id></p><n xsi:nil="true"/></r>')
    out = element_to_python(ET.fromstring(xml))
    assert out["p"] == [{"uri": "u1", "id": "a"}, {"uri": "u2", "id": "b"}]
    assert out["n"] is None
