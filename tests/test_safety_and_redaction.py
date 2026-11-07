import logging

import httpx
import pytest

from pavs.domain.enums import EnvironmentKind
from pavs.domain.errors import WriteNotAuthorized
from pavs.domain.models import TlsSettings
from pavs.infra.logging_setup import RedactingFilter
from pavs.infra.redaction import REDACTED, SecretRegistry, redact_headers, redact_text, scrub
from pavs.polarion.safety import WriteGuard
from pavs.polarion.transport import HttpTransport

from .conftest import make_env


def test_reads_always_pass():
    WriteGuard(make_env()).check("GET", "http://x/polarion/rest/v1/projects/p", None)


def test_write_without_authorization_is_blocked():
    with pytest.raises(WriteNotAuthorized, match="Read-only"):
        WriteGuard(make_env()).check("PATCH", "http://x/polarion/rest/v1/projects/acme_test/workitems/A-1", None)


def test_write_only_in_authorized_test_project():
    g = WriteGuard(make_env())
    auth = g.issue("acme_test", run_id="r1", approved_by="tester")
    g.check("PATCH", "http://x/polarion/rest/v1/projects/acme_test/workitems/A-1", auth)
    with pytest.raises(WriteNotAuthorized, match="only allowed"):
        g.check("PATCH", "http://x/polarion/rest/v1/projects/acme_demo/workitems/A-1", auth)
    with pytest.raises(WriteNotAuthorized, match="global"):
        g.check("POST", "http://x/polarion/rest/v1/users", auth)
    with pytest.raises(WriteNotAuthorized):
        g.issue("acme_demo", run_id="r1", approved_by="tester")


def test_foreign_or_revoked_authorization_is_rejected():
    g1, g2 = WriteGuard(make_env()), WriteGuard(make_env())
    auth = g1.issue("acme_test", "r1", "tester")
    with pytest.raises(WriteNotAuthorized, match="not issued"):
        g2.check("DELETE", "http://x/polarion/rest/v1/projects/acme_test/workitems/A-1", auth)
    g1.revoke(auth)
    with pytest.raises(WriteNotAuthorized):
        g1.check("DELETE", "http://x/polarion/rest/v1/projects/acme_test/workitems/A-1", auth)


def test_production_needs_typed_confirmation():
    env = make_env(name="Prod", kind=EnvironmentKind.PRODUCTION)
    g = WriteGuard(env)
    with pytest.raises(WriteNotAuthorized, match="production"):
        g.issue("acme_test", "r1", "lead")
    assert g.issue("acme_test", "r1", "lead", production_confirmation="Prod").production_override


def test_transport_enforces_guard_before_sending():
    sent = []
    t = HttpTransport(TlsSettings(), 5, WriteGuard(make_env()),
                      transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    with pytest.raises(WriteNotAuthorized):
        t.request("POST", "http://x/polarion/rest/v1/projects/acme_test/workitems", provider="rest", json={})
    assert sent == []


def test_redaction_of_headers_bodies_and_text():
    assert redact_headers({"Authorization": "Bearer abc", "Accept": "x"})["Authorization"] == REDACTED
    soap = "<ns:logIn><ns:userName>u</ns:userName><ns:password>s3cret!</ns:password></ns:logIn>"
    assert "s3cret" not in redact_text(soap)
    tok = '<ses:token>abcdef123</ses:token><ns1:sessionID a="b">12345</ns1:sessionID>'
    out = redact_text(tok)
    assert "abcdef123" not in out and "12345" not in out
    assert "xyz" not in redact_text('{"password": "xyz", "a": 1}')
    assert "tok123456" not in redact_text("Authorization: Bearer tok123456")
    assert "pw1" not in redact_text("https://h/x?password=pw1&y=2")


def test_registered_secrets_are_scrubbed_everywhere():
    SecretRegistry.register("my-very-secret-token")
    assert "my-very-secret-token" not in scrub("error while using my-very-secret-token here")


def test_log_filter_scrubs_messages():
    SecretRegistry.register("hunter2-secret")
    rec = logging.LogRecord("pavs.x", logging.INFO, __file__, 1, "token is %s", ("hunter2-secret",), None)
    RedactingFilter().filter(rec)
    assert "hunter2-secret" not in rec.getMessage()


def test_recorded_exchange_is_redacted():
    t = HttpTransport(TlsSettings(), 5, WriteGuard(make_env()),
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"token": "leak-me"})))
    t.request("GET", "http://x/polarion/rest/v1/user", provider="rest",
              headers={"Authorization": "Bearer super-secret-1"})
    ex = t.recorder.items()[0]
    assert ex.request_headers["authorization"] == REDACTED
    assert "leak-me" not in (ex.response_body or "")
