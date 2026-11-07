"""Test fixtures. Tests never touch the network, the real vault, or the user's data dir."""
from __future__ import annotations

import httpx
import pytest

from mock_polarion import PROFILES, MockPolarion
from pavs.domain.enums import AuthMethod, EnvironmentKind, ProviderKind
from pavs.domain.models import Credentials, Environment
from pavs.infra.redaction import SecretRegistry

MOCK_URL = "http://mock.test/polarion"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PAVS_DATA_DIR", str(tmp_path / "data"))

    def _no_keyring(*a, **k):  # pragma: no cover - fails loudly if a test reaches the OS store
        raise AssertionError("tests must not use the OS credential store")

    import keyring
    monkeypatch.setattr(keyring, "set_password", _no_keyring)
    monkeypatch.setattr(keyring, "get_password", _no_keyring)
    SecretRegistry.clear()
    yield
    SecretRegistry.clear()


SPAWNED: list[str] = []


@pytest.fixture(autouse=True)
def _no_real_programs(monkeypatch):
    """Tests must never start TortoiseSVN (or anything else) on the desktop: commands are only recorded."""
    SPAWNED.clear()
    import pavs.integrations.tortoisesvn as t
    monkeypatch.setattr(t, "_spawn", lambda line: SPAWNED.append(line))
    yield


def make_env(**kw) -> Environment:
    defaults = dict(name="Mock", base_url=MOCK_URL, kind=EnvironmentKind.TEST,
                    auth_method=AuthMethod.TOKEN, username="admin",
                    providers_enabled=[ProviderKind.REST, ProviderKind.SOAP],
                    test_projects=["acme_test"])
    defaults.update(kw)
    return Environment(**defaults)


@pytest.fixture
def mock_server():
    """Factory: mock_server('2606') -> (MockPolarion app, httpx transport)."""
    def factory(profile: str):
        app = MockPolarion(PROFILES[profile])
        return app, httpx.WSGITransport(app=app)
    return factory


ADMIN_TOKEN = Credentials(token="test-token-admin")
ADMIN_PASSWORD = Credentials(password="admin-test-pw")
