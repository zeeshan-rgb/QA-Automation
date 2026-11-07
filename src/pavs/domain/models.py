"""Pydantic models shared by all layers."""
from __future__ import annotations

import re
from urllib.parse import unquote

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .enums import (AuthMethod, CapabilityStatus, EnvironmentKind, ProviderKind,
                    ProviderState, Verification)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TlsSettings(BaseModel):
    verify: bool = True
    ca_bundle: str | None = None       # path to a corporate CA bundle (PEM)


_PROJECT_IN_URL = re.compile(r"[#/]project/([^/?#]+)", re.IGNORECASE)


def split_polarion_url(url: str) -> tuple[str, str | None]:
    """(base address ending in /polarion, project id found in the address or None).

    People paste the address of a page they were looking at, e.g.
    http://host/polarion/#/project/Vision/home. Everything after /polarion is dropped, and the project id is kept
    so the app can preselect it."""
    v = (url or "").strip()
    if not v.lower().startswith(("http://", "https://")):
        raise ValueError("Base URL must start with http:// or https://")
    project = None
    m = _PROJECT_IN_URL.search(v.split("/polarion", 1)[1] if "/polarion" in v.lower() else "")
    if m:
        project = unquote(m.group(1))
    low = v.lower()
    idx = low.find("/polarion")
    # cut at the first "/polarion" that ends a path segment ("/polarionx" is not it)
    while idx != -1 and idx + 9 < len(v) and v[idx + 9] not in "/?#":
        idx = low.find("/polarion", idx + 1)
    if idx != -1:
        base = v[: idx + 9]
    else:
        base = v.split("#", 1)[0].split("?", 1)[0].rstrip("/") + "/polarion"
    return base, project


class Environment(BaseModel):
    """A saved Polarion instance. Holds no secrets, only a credential reference."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    base_url: str                      # e.g. https://polarion-dev.example.local/polarion
    kind: EnvironmentKind = EnvironmentKind.DEVELOPMENT
    auth_method: AuthMethod = AuthMethod.TOKEN
    username: str | None = None        # needed for password auth; informational for token auth
    tls: TlsSettings = Field(default_factory=TlsSettings)
    timeout_seconds: float = 30.0
    providers_enabled: list[ProviderKind] = Field(
        default_factory=lambda: [ProviderKind.REST, ProviderKind.SOAP])
    test_projects: list[str] = Field(default_factory=list)   # the only projects writes may touch
    # role id -> Polarion user name of a test account holding that role (its token lives in the vault)
    test_accounts: dict[str, str] = Field(default_factory=dict)
    notes: str = ""
    local_repo_path: str | None = None   # Polarion on THIS computer: read the repository files directly (read-only)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("base_url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        return split_polarion_url(v)[0]

    @field_validator("name")
    @classmethod
    def _name_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @property
    def server_root(self) -> str:
        """URL without the /polarion suffix (where /repo lives)."""
        return self.base_url[: -len("/polarion")]

    @property
    def credential_key(self) -> str:
        return f"env:{self.id}"


class Credentials(BaseModel):
    """Secrets for one environment. Never persisted outside the vault, never logged."""
    token: str | None = None
    password: str | None = None
    svn_user: str | None = None          # only when the repository (/repo) login differs from the Polarion login
    svn_password: str | None = None

    def __repr__(self) -> str:   # pragma: no cover - defensive
        return "Credentials(<redacted>)"

    __str__ = __repr__


class VersionSignal(BaseModel):
    source: str
    value: str                          # raw value found
    release: str | None = None          # parsed release, e.g. "2606"
    confidence: float


class VersionInfo(BaseModel):
    release: str | None = None          # "2606"; None = unknown
    internal_version: str | None = None  # "3.26.6"
    build: str | None = None
    confidence: float = 0.0
    signals: list[VersionSignal] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    kind: ProviderKind
    state: ProviderState
    detail: str = ""


class CapabilityRecord(BaseModel):
    capability_id: str
    title: str
    area: str
    status: CapabilityStatus
    verification: Verification
    provider: ProviderKind | None = None
    strategy: str = ""
    detail: str = ""
    checked_at: datetime = Field(default_factory=utcnow)


class ProjectInfo(BaseModel):
    id: str
    name: str = ""
    active: bool | None = None


class UserInfo(BaseModel):
    id: str
    name: str = ""


class Exchange(BaseModel):
    """One redacted HTTP exchange, kept as evidence."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    provider: str = ""
    method: str
    url: str
    status: int | None = None
    duration_ms: float | None = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str | None = None
    error: str | None = None
    at: datetime = Field(default_factory=utcnow)


class ServerProfile(BaseModel):
    """Everything learned while connecting to an environment."""
    environment_id: str
    connected_at: datetime = Field(default_factory=utcnow)
    reachable: bool = False
    version: VersionInfo = Field(default_factory=VersionInfo)
    adapter: str = ""
    providers: list[ProviderStatus] = Field(default_factory=list)
    current_user: UserInfo | None = None
    projects: list[ProjectInfo] = Field(default_factory=list)
    capabilities: list[CapabilityRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    exchanges: list[Exchange] = Field(default_factory=list)

    def provider(self, kind: ProviderKind) -> ProviderStatus | None:
        return next((p for p in self.providers if p.kind == kind), None)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for c in self.capabilities:
            counts[c.status.value] = counts.get(c.status.value, 0) + 1
        return {"release": self.version.release, "adapter": self.adapter,
                "projects": len(self.projects), "capabilities": counts}
