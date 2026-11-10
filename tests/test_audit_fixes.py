"""Regression tests for defects found in the production-readiness audit (docs/AUDIT-2026-11.md)."""
import httpx
import pytest

from pavs.domain.errors import SoapFault
from pavs.domain.models import Environment, TlsSettings
from pavs.polarion.rest.client import RestClient
from pavs.polarion.safety import WriteGuard
from pavs.polarion.soap.client import SoapClient
from pavs.polarion.transport import HttpTransport


def _transport(handler):
    env = Environment(name="t", base_url="https://polarion.example.com/polarion")
    return HttpTransport(TlsSettings(), 10, WriteGuard(env), transport=httpx.MockTransport(handler)), env


# ---------------------------------------------------------------- A1: the bearer token must never leave the configured host
def test_rest_paging_never_follows_a_next_link_to_another_host():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.host, request.url.params.get("page[number]"), request.headers.get("authorization")))
        page = int(request.url.params.get("page[number]", "1"))
        body = {"data": [{"id": f"p{page}"}]}
        if page < 3:
            body["links"] = {"next": f"https://attacker.example/steal?page%5Bnumber%5D={page + 1}"}
        return httpx.Response(200, json=body)

    t, env = _transport(handler)
    rest = RestClient(t, env.base_url, "secret-token-value")
    assert [i["id"] for i in rest.iterate("/projects", page_size=1)] == ["p1", "p2", "p3"]
    assert {h for h, _, _ in seen} == {"polarion.example.com"}                         # nothing was sent elsewhere
    assert [n for _, n, _ in seen] == ["1", "2", "3"]                                  # paged by number on our own server


def test_rest_paging_still_follows_a_same_host_next_link():
    seen = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        page = int(request.url.params.get("page[number]", "1"))
        body = {"data": [{"id": f"p{page}"}]}
        if page < 2:
            body["links"] = {"next": f"https://polarion.example.com/polarion/rest/v1/projects?page%5Bsize%5D=1&page%5Bnumber%5D={page + 1}"}
        return httpx.Response(200, json=body)

    t, env = _transport(handler)
    assert [i["id"] for i in RestClient(t, env.base_url, "tok-12345").iterate("/projects", page_size=1)] == ["p1", "p2"]


# ---------------------------------------------------------------- A2: SOAP session that expired on the server
def _soap_envelope(body: str) -> str:
    return ('<?xml version="1.0"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Header>'
            '<sessionID xmlns="http://ws.polarion.com/session">111</sessionID></soapenv:Header>'
            f"<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>")


FAULT = ('<soapenv:Fault><faultcode>soapenv:Server.generalException</faultcode><faultstring>Not authorized.</faultstring></soapenv:Fault>')


def _soap_client(has_subject: str):
    calls = []

    def handler(request: httpx.Request):
        op = request.content.decode().split("<ns:")[1].split(">")[0].split("/")[0].split(" ")[0]
        calls.append(op)
        if op in ("getGlobalRoles", "createWorkItem"):
            n = calls.count(op)
            return httpx.Response(500, text=_soap_envelope(FAULT)) if n == 1 else httpx.Response(
                200, text=_soap_envelope('<getGlobalRolesResponse xmlns="x"><getGlobalRolesReturn>admin</getGlobalRolesReturn></getGlobalRolesResponse>'))
        if op == "hasSubject":
            return httpx.Response(200, text=_soap_envelope(f'<hasSubjectResponse xmlns="x"><hasSubjectReturn>{has_subject}</hasSubjectReturn></hasSubjectResponse>'))
        return httpx.Response(200, text=_soap_envelope(f'<{op}Response xmlns="x"/>'))

    t, env = _transport(handler)
    soap = SoapClient(t, env.base_url)
    return soap, calls


def test_expired_soap_session_signs_in_again_and_repeats_a_read_once():
    soap, calls = _soap_client("false")
    soap.set_reauth(lambda: soap.login_password("u", "p"))
    out = soap.call("Security", "getGlobalRoles")
    assert "admin" in str(out)
    assert calls == ["getGlobalRoles", "hasSubject", "logIn", "getGlobalRoles"]


def test_a_genuine_permission_denial_is_not_retried_or_re_signed_in():
    soap, calls = _soap_client("true")                        # the session is fine: the user simply may not do this
    soap.set_reauth(lambda: soap.login_password("u", "p"))
    with pytest.raises(SoapFault, match="Not authorized"):
        soap.call("Security", "getGlobalRoles")
    assert calls == ["getGlobalRoles", "hasSubject"]


def test_a_write_is_never_repeated_automatically():
    from pavs.domain.errors import WriteNotAuthorized
    soap, calls = _soap_client("false")
    soap.set_reauth(lambda: soap.login_password("u", "p"))
    with pytest.raises(WriteNotAuthorized):                   # no authorization: the guard stops it before any request
        soap.call("Tracker", "createWorkItem", {"x": "y"})
    assert calls == []


def test_without_a_reauth_hook_the_fault_is_raised_unchanged():
    soap, calls = _soap_client("false")
    with pytest.raises(SoapFault):
        soap.call("Security", "getGlobalRoles")
    assert calls == ["getGlobalRoles"]


def test_session_expiry_end_to_end_on_the_mock(tmp_path, mock_server):
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend
    app, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "se.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    out = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "")
    env = out.environment
    first = ctx.testing.run(env.id, "acme_demo", health=True)
    app.sessions.clear()                                      # the server forgets the session (timeout)
    second = ctx.testing.run(env.id, "acme_demo", health=True)
    assert second.counts() == first.counts() and not any("Not authorized" in g for g in second.inspection_gaps)
    ctx.close()


# ---------------------------------------------------------------- B: requirement -> check mapping (false-positive risks)
def _generate(sentence, roles=("internal_reviewer", "project_admin", "project_user")):
    from pavs.domain.requirements import Requirement
    from pavs.engines.requirements.rules import Names, generate_for_requirement
    return generate_for_requirement(Requirement(external_id="", title="t", text=sentence), Names(roles=list(roles)))


def test_except_is_never_turned_into_the_opposite_check():
    g = _generate("Everyone except project_user can delete Work Items.")
    assert g.cases == [] and any(f.kind.value == "not_automatable" and "except" in f.message for f in g.findings)


def test_only_keeps_the_positive_check_and_says_the_exclusive_part_is_untested():
    g = _generate("Only project_admin can delete Work Items.")
    assert [c.check.expect.value for c in g.cases] == ["granted"]
    assert any(f.kind.value == "ambiguous" and "OTHER role" in f.message for f in g.findings)


def test_comment_on_is_a_verb_not_the_comment_object():
    g = _generate("The internal_reviewer role can read and comment on Work Items but cannot modify them.")
    perms = {c.check.target["permission"].rsplit(".", 2)[-2] + "." + c.check.target["permission"].rsplit(".", 1)[-1] for c in g.cases}
    assert perms == {"WorkItem.read", "Comment.create", "WorkItem.modify"}


def test_a_role_that_must_not_exist_is_an_absence_check_with_the_right_name():
    g = _generate("The role project_admin must not exist.")
    assert [(c.check.expect.value, c.check.target["role"]) for c in g.cases] == [("absent", "project_admin")]


def test_a_requires_sentence_is_reported_instead_of_silently_dropped():
    g = _generate("Approval requires two reviewers.")
    assert g.cases == [] and any(f.kind.value == "not_automatable" for f in g.findings)


def test_absence_check_runs_against_the_real_role_list(tmp_path, mock_server):
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "ab.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    env = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "test-token-admin").environment
    a = ctx.testing.import_pasted("The role project_admin must not exist. The role ghost_role_xyz must not exist.", "t", env.id, "acme_demo")
    run = ctx.testing.run(env.id, "acme_demo", health=False, set_id=a.requirement_set.id)
    by = {r.name: r.status.value for r in run.results}
    assert by["Role 'project_admin' does not exist"] == "fail" and by["Role 'ghost_role_xyz' does not exist"] == "pass"
    ctx.close()


# ---------------------------------------------------------------- C: credential store size limit (Windows Credential Manager)
class _SmallBackend:
    """Behaves like Windows Credential Manager: an entry above ~1,250 characters is refused."""
    secure = True
    LIMIT = 1250

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        if len(value) > self.LIMIT:
            raise OSError("The stub received bad data")
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


def test_a_long_jwt_is_stored_in_pieces_and_read_back_intact():
    from pavs.domain.models import Credentials
    from pavs.infra.vault import CredentialVault
    backend = _SmallBackend()
    vault = CredentialVault(backend)
    jwt = "eyJhbGciOiJSUzI1NiJ9." + "A" * 2400 + "." + "B" * 300
    creds = Credentials(token=jwt, password="pw-" * 10, svn_user="svn", svn_password="C" * 100)
    vault.save("env:1", creds)
    assert len(backend.data) > 2 and all(len(v) <= backend.LIMIT for v in backend.data.values())
    assert vault.load("env:1") == creds
    vault.save("env:1", Credentials(token="short-token-1"))             # shrinking leaves no orphan pieces behind
    assert sorted(backend.data) == ["env:1"] and vault.load("env:1").token == "short-token-1"
    vault.save("env:1", creds)
    vault.delete("env:1")
    assert backend.data == {} and vault.load("env:1") is None


def test_small_credentials_are_stored_exactly_as_before():
    from pavs.domain.models import Credentials
    from pavs.infra.vault import CredentialVault
    backend = _SmallBackend()
    CredentialVault(backend).save("k", Credentials(token="abc-token-123", password="pw12345"))
    assert list(backend.data) == ["k"] and backend.data["k"].startswith("{")        # backward compatible format


def test_a_missing_piece_is_reported_not_returned_half_read():
    from pavs.domain.errors import CredentialError
    from pavs.domain.models import Credentials
    from pavs.infra.vault import CredentialVault
    backend = _SmallBackend()
    vault = CredentialVault(backend)
    vault.save("k", Credentials(token="x" * 3000))
    del backend.data["k#1"]
    with pytest.raises(CredentialError, match="incomplete"):
        vault.load("k")


# ---------------------------------------------------------------- D: a redirecting server is explained, not misreported
def test_http_to_https_redirect_tells_the_person_what_to_enter():
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend

    def handler(request: httpx.Request):
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://polarion.example.com/polarion/"})
        return httpx.Response(200, text="<html>Version 2606</html>")

    sent = []
    c = build_context(db_path=":memory:", vault_backend=MemoryBackend(), log_to_file=False,
                      http_transport=httpx.MockTransport(lambda r: (sent.append(r), handler(r))[1]))
    out = c.connections.quick_connect("http://polarion.example.com/polarion", "admin", "pw-secret-1", "tok-secret-1")
    assert not out.ok and "redirects this address to https://polarion.example.com/polarion/" in out.message
    assert all(r.url.scheme == "http" for r in sent)                         # credentials were never sent to the redirect target
    assert not any(r.headers.get("authorization") for r in sent)
    c.close()


# ---------------------------------------------------------------- E: project roles over SOAP (found on a real Polarion 2606)
def test_project_location_is_the_folder_not_the_descriptor_file():
    from pavs.engines.inspection.readers_roles import context_location
    assert context_location("default:/Vision/.polarion/polarion-project.xml") == "default:/Vision"
    assert context_location("default:/Demo Projects/drivepilot/.polarion/polarion-project.xml") == "default:/Demo Projects/drivepilot"
    assert context_location("default:/Vision") == "default:/Vision" and context_location(None) is None and context_location("") is None


def test_project_roles_are_read_over_soap_alone(tmp_path, mock_server):
    """The mock now refuses the descriptor-file location exactly as the real server does ('Location is not a valid context')."""
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend
    _, wsgi = mock_server("2410-rest-off")
    ctx = build_context(db_path=tmp_path / "roles.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    out = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "", read_config_files=False)
    sid, snap = ctx.inspection.inspect(out.environment.id, "acme_demo", sample_items=False)
    roles = next(s for s in snap.sections if s.area == "roles")
    ids = {r["id"] for r in roles.items["roles"] if r["scope"] == "project"}
    assert {"project_admin", "internal_reviewer"} <= ids                         # read through getContextRoles, no SVN
    assert not any("not a valid context" in " ".join(roles.errors) for _ in [0])
    ctx.close()


def test_rich_pages_are_listed_from_the_repository_when_soap_cannot_see_them(tmp_path, mock_server):
    """Real 2606: SOAP getWikiSpaces returned nothing for a project whose repository holds several Rich Pages."""
    from pavs.app import build_context
    from pavs.infra.vault import MemoryBackend
    _, wsgi = mock_server("2410-rest-off")
    ctx = build_context(db_path=tmp_path / "pg.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    out = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "")
    _, snap = ctx.inspection.inspect(out.environment.id, "acme_demo", sample_items=False)
    pages = next(s for s in snap.sections if s.area == "pages").items["pages"]
    assert any(p["space"] == "Reports" and p["name"] == "Coverage" for p in pages)
    out = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "", read_config_files=False)
    _, snap = ctx.inspection.inspect(out.environment.id, "acme_test", sample_items=False)
    sec = next(s for s in snap.sections if s.area == "pages")
    assert sec.items["pages"] == [] and sec.status.value == "partial" and any("Rich Pages" in g.item for g in sec.gaps)   # said, not hidden
    ctx.close()


def test_neither_nor_is_a_denial_not_a_grant():
    g = _generate("The approver can neither modify nor delete approved Requirements.", roles=("approver",))
    assert sorted(c.check.expect.value for c in g.cases) == ["denied", "denied"]


def test_a_status_change_sentence_is_not_read_as_a_permission_on_the_status_name():
    g = _generate("Defects can be reopened from Closed to Open.")
    assert g.cases == [] and any(f.kind.value == "not_automatable" for f in g.findings)


def test_a_link_role_named_after_the_word_role_gets_its_real_name():
    g = _generate("Requirements are linked to Test Cases with the role verifies.")
    assert [c.check.target["link_role"] for c in g.cases] == ["verifies"]


def test_connecting_from_the_connect_page_keeps_the_corporate_ca_bundle(tmp_path, mock_server):
    """quick_connect used to rebuild TlsSettings from the checkbox alone, silently dropping a CA bundle saved in the environment dialog."""
    from pavs.app import build_context
    from pavs.domain.models import TlsSettings
    from pavs.infra.vault import MemoryBackend
    _, wsgi = mock_server("2606")
    ctx = build_context(db_path=tmp_path / "ca.sqlite3", vault_backend=MemoryBackend(), http_transport=wsgi, log_to_file=False)
    env = ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "test-token-admin").environment
    ctx.environments.save(env.model_copy(update={"tls": TlsSettings(verify=True, ca_bundle="C:/certs/corp-ca.pem")}))
    ctx.connections.quick_connect("https://mock.test/polarion", "admin", "admin-test-pw", "test-token-admin")      # connect again from the page
    assert ctx.environments.get(env.id).tls.ca_bundle == "C:/certs/corp-ca.pem"
    ctx.close()
