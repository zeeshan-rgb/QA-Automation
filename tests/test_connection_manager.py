from pavs.domain.enums import (AuthMethod, CapabilityStatus as S, ProviderKind as P,
                              ProviderState as St, Verification as V)
from pavs.domain.models import Credentials
from pavs.infra.redaction import REDACTED
from pavs.polarion.connection import ConnectionManager

from .conftest import ADMIN_PASSWORD, ADMIN_TOKEN, make_env


def _connect(mock_server, profile, env, creds):
    app, wsgi = mock_server(profile)
    session = ConnectionManager(http_transport=wsgi).connect(env, creds)
    return app, session, session.profile


def _caps(profile):
    return {c.capability_id: c for c in profile.capabilities}


def test_2606_with_token(mock_server):
    _, s, p = _connect(mock_server, "2606", make_env(), ADMIN_TOKEN)
    assert p.reachable and p.version.release == "2606" and p.version.confidence == 1.0
    assert {sig.source for sig in p.version.signals} >= {"login-page buildId", "rest /metadata"}
    assert p.adapter == "Polarion 2606"
    assert p.provider(P.REST).state == St.AUTHENTICATED
    assert p.provider(P.SOAP).state == St.AUTHENTICATED
    assert p.current_user.id == "admin"
    assert [x.id for x in p.projects] == ["acme_demo", "acme_test"]
    caps = _caps(p)
    assert caps["projects.list"].verification == V.VERIFIED
    assert caps["server.metadata"].status == S.SUPPORTED
    assert caps["customfields.definitions"].provider == P.REST
    assert caps["roles.global"].provider == P.SOAP and caps["roles.global"].verification == V.VERIFIED
    s.close()


def test_2410_rest_off_like_the_client(mock_server):
    env = make_env(auth_method=AuthMethod.PASSWORD)
    _, s, p = _connect(mock_server, "2410-rest-off", env, ADMIN_PASSWORD)
    assert p.version.release == "2410" and p.adapter == "Polarion 2410"
    assert p.provider(P.REST).state == St.DISABLED
    assert "switched off" in p.provider(P.REST).detail
    assert p.provider(P.SOAP).state == St.AUTHENTICATED
    assert [x.id for x in p.projects] == ["acme_demo", "acme_test"]          # via SOAP
    caps = _caps(p)
    assert caps["workitems.query"].provider == P.SOAP
    assert caps["customfields.definitions"].provider == P.SOAP
    assert caps["server.metadata"].status == S.UNSUPPORTED
    assert any("2512" in w or "validated" in w for w in p.warnings)
    s.close()


def test_2512_without_swagger_uses_catalog(mock_server):
    from mock_polarion import PROFILES
    PROFILES["2512"].swagger_enabled = False
    try:
        _, s, p = _connect(mock_server, "2512", make_env(), ADMIN_TOKEN)
    finally:
        PROFILES["2512"].swagger_enabled = True
    caps = _caps(p)
    assert p.version.release == "2512"
    assert caps["fields.metadata"].verification in (V.DOCUMENTED, V.VERIFIED)
    assert "llm.completion" not in caps
    s.close()


def test_anonymous_detection_only(mock_server):
    env = make_env(auth_method=AuthMethod.NONE, providers_enabled=[P.REST, P.SOAP, P.SVN])
    _, s, p = _connect(mock_server, "2506", env, None)
    assert p.version.release == "2506"
    assert p.provider(P.REST).state == St.AVAILABLE
    assert p.provider(P.SVN).state == St.AVAILABLE
    assert all(c.status in (S.NEEDS_PERMISSION, S.UNSUPPORTED, S.UNKNOWN) for c in p.capabilities)
    assert p.projects == []
    s.close()


def test_bad_token_reports_auth_failed(mock_server):
    _, s, p = _connect(mock_server, "2606", make_env(), Credentials(token="nope-nope"))
    assert p.provider(P.REST).state == St.AUTH_FAILED
    assert p.provider(P.SOAP).state == St.AUTH_FAILED
    assert _caps(p)["projects.list"].status == S.NEEDS_PERMISSION
    s.close()


def test_rest_permission_denied_user(mock_server):
    _, s, p = _connect(mock_server, "2606", make_env(), Credentials(token="test-token-reviewer"))
    assert "lacks REST endpoint permissions" in p.provider(P.REST).detail
    assert _caps(p)["users.current"].status in (S.SUPPORTED, S.NEEDS_PERMISSION)
    s.close()


def test_svn_with_password(mock_server):
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=[P.REST, P.SOAP, P.SVN])
    _, s, p = _connect(mock_server, "2606", env, Credentials(token="test-token-admin", password="admin-test-pw"))
    assert p.provider(P.SVN).state == St.AUTHENTICATED
    assert _caps(p)["permissions.config"].provider == P.SVN
    s.close()


def test_unreachable_server_is_reported_not_raised():
    import httpx

    def refuse(req):
        raise httpx.ConnectError("connection refused")
    s = ConnectionManager(http_transport=httpx.MockTransport(refuse)).connect(make_env(), ADMIN_TOKEN)
    assert not s.profile.reachable
    assert s.profile.errors and "not reachable" in s.profile.errors[0]
    assert all(pr.state == St.UNREACHABLE for pr in s.profile.providers)
    s.close()


def test_no_secret_in_evidence(mock_server):
    env = make_env(auth_method=AuthMethod.TOKEN_AND_PASSWORD, providers_enabled=[P.REST, P.SOAP, P.SVN])
    _, s, p = _connect(mock_server, "2606", env, Credentials(token="test-token-admin", password="admin-test-pw"))
    dump = p.model_dump_json()
    assert "test-token-admin" not in dump and "admin-test-pw" not in dump
    assert REDACTED in dump
    s.close()
