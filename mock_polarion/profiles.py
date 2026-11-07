"""Release profiles for the mock server. Values are obvious fakes (no real data)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MockUser:
    id: str
    name: str
    password: str | None = None
    token: str | None = None
    rest_allowed: bool = True          # REST endpoint permissions (403 if False)
    denied_verbs: tuple = ()           # HTTP verbs this account has no REST endpoint permission for
    global_roles: tuple[str, ...] = ("user",)


@dataclass
class MockProfile:
    key: str
    release: str                        # "2606"
    build_counter: str = "0001"
    rest_enabled: bool = True
    swagger_enabled: bool = True        # serves /rest/v1/definition
    soap_enabled: bool = True
    svn_enabled: bool = True
    users: list[MockUser] = field(default_factory=list)
    projects: list[tuple[str, str]] = field(default_factory=list)   # (id, name)

    @property
    def internal(self) -> str:
        return f"3.{self.release[:2]}.{int(self.release[2:])}"

    @property
    def build_id(self) -> str:
        return f"20260101-1200-{self.release}.{self.build_counter}-0123abcd"


def _users() -> list[MockUser]:
    return [
        MockUser("admin", "System Administrator", password="admin-test-pw", token="test-token-admin",
                 global_roles=("admin", "user")),
        MockUser("reviewer1", "Test Reviewer", password="reviewer-test-pw", token="test-token-reviewer",
                 rest_allowed=False),
        MockUser("tester1", "Test Account (reviewer)", password="tester-test-pw", token="test-token-tester"),
        MockUser("dev1", "Test Account (assignable)", password="dev-test-pw", token="test-token-dev"),
        MockUser("roapi1", "Test Account (GET only)", password="roapi-test-pw", token="test-token-roapi",
                 denied_verbs=("POST", "PATCH", "DELETE")),
    ]


def _projects() -> list[tuple[str, str]]:
    return [("acme_demo", "Acme Demo"), ("acme_test", "Acme Test Sandbox")]


def _profile(key: str, release: str, **kw) -> MockProfile:
    return MockProfile(key=key, release=release, users=_users(), projects=_projects(), **kw)


PROFILES: dict[str, MockProfile] = {
    "2410-rest-off": _profile("2410-rest-off", "2410", rest_enabled=False, swagger_enabled=False),
    "2410": _profile("2410", "2410", swagger_enabled=False),
    "2506": _profile("2506", "2506"),
    "2512": _profile("2512", "2512"),
    "2606": _profile("2606", "2606"),
}
