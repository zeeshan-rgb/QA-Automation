"""Write Guard: the only place that can allow a non-read request.

Read-only is the default. A write needs a WriteAuthorization that was issued for this
environment, a project in the environment's allowed test projects, and a run id. On a
production environment it also needs the environment name typed as confirmation.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..domain.enums import EnvironmentKind
from ..domain.errors import WriteNotAuthorized
from ..domain.models import Environment

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND"})
_PROJECT_IN_PATH = re.compile(r"/rest/v1/projects/([^/?#]+)")


@dataclass(frozen=True)
class WriteAuthorization:
    environment_id: str
    project_id: str
    run_id: str
    approved_by: str
    expires_at: datetime
    production_override: bool = False
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class WriteGuard:
    def __init__(self, environment: Environment, audit=None) -> None:
        self.environment = environment
        self.audit = audit
        self._issued: set[str] = set()

    # ------------------------------------------------------------------ issuing
    def issue(self, project_id: str, run_id: str, approved_by: str,
              production_confirmation: str | None = None,
              ttl: timedelta = timedelta(hours=2)) -> WriteAuthorization:
        env = self.environment
        if project_id not in env.test_projects:
            self._audit("write.authorize", "refused", project_id,
                        "project is not in the environment's test projects")
            raise WriteNotAuthorized(
                f"Project '{project_id}' is not an allowed test project for '{env.name}'.")
        override = False
        if env.kind == EnvironmentKind.PRODUCTION:
            if production_confirmation != env.name:
                self._audit("write.authorize", "refused", project_id, "production confirmation missing")
                raise WriteNotAuthorized(
                    f"'{env.name}' is a production environment. Type its exact name to confirm writes.")
            override = True
        auth = WriteAuthorization(environment_id=env.id, project_id=project_id, run_id=run_id,
                                  approved_by=approved_by, production_override=override,
                                  expires_at=datetime.now(timezone.utc) + ttl)
        self._issued.add(auth.token)
        self._audit("write.authorize", "granted", project_id,
                    {"run_id": run_id, "approved_by": approved_by, "production": override})
        return auth

    def revoke(self, auth: WriteAuthorization) -> None:
        self._issued.discard(auth.token)

    # ------------------------------------------------------------------ checking
    def check(self, method: str, url: str, auth: WriteAuthorization | None,
              is_write: bool | None = None, project_id: str | None = None) -> None:
        """Raise WriteNotAuthorized unless the request is a read or is properly authorized."""
        write = (method.upper() not in READ_METHODS) if is_write is None else is_write
        if not write:
            return
        reason = self._refusal(url, auth, project_id)
        if reason:
            self._audit("write.blocked", "refused", project_id or url, {"method": method, "reason": reason})
            raise WriteNotAuthorized(reason)

    def _refusal(self, url: str, auth: WriteAuthorization | None, project_id: str | None) -> str | None:
        if auth is None:
            return "Read-only mode: this request changes data and no write authorization was given."
        if auth.token not in self._issued:
            return "The write authorization was not issued by this guard (or was revoked)."
        if auth.environment_id != self.environment.id:
            return "The write authorization belongs to another environment."
        if auth.expired():
            return "The write authorization has expired."
        target = project_id
        if target is None:
            m = _PROJECT_IN_PATH.search(url)
            target = m.group(1) if m else None
        if target is None:
            return "Writes outside a project (global endpoints) are never allowed."
        if target != auth.project_id or target not in self.environment.test_projects:
            return f"Writes are only allowed in the authorized test project '{auth.project_id}'."
        return None

    def _audit(self, action: str, outcome: str, target, detail) -> None:
        if self.audit is not None:
            self.audit.record(action, outcome, environment_id=self.environment.id,
                              target=str(target), detail=detail)
