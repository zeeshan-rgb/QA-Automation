"""Application roles: Viewer < Tester < Lead < Admin.

This is an operating guardrail for a desktop tool, not a security boundary against someone who owns the
machine. It stops a Viewer from running tests, a Tester from writing test data and nobody but an Admin from
touching production settings. The Polarion side is unchanged: the Write Guard still decides what may be written
and Polarion's own permissions still decide what the logged-in account may do.

The role lives in the local database. Lowering it is always free. Raising it asks for the role passphrase once one
has been set (stored as a salted PBKDF2 hash, never in clear text).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from enum import Enum

from ..domain.errors import PavsError
from ..infra.storage import AuditLog, Database


class RoleDenied(PavsError):
    """The current application role does not allow this action."""


class AppRole(str, Enum):
    VIEWER = "viewer"
    TESTER = "tester"
    LEAD = "lead"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return list(AppRole).index(self)

    @property
    def label(self) -> str:
        return self.value.capitalize()


class Action(str, Enum):
    VIEW = "view results, history and reports"
    RUN_TESTS = "connect to Polarion and run read-only tests"
    WRITE_TESTS = "run behavioural tests that create test data, or clean it up"
    APPROVE_WRITES = "approve test projects and test accounts"
    SVN_WRITE_TOOLS = "start a TortoiseSVN checkout, merge or commit"
    MANAGE_ENVIRONMENTS = "change production environments or delete environments"
    MANAGE_ROLES = "set the role passphrase"


MINIMUM = {
    Action.VIEW: AppRole.VIEWER,
    Action.RUN_TESTS: AppRole.TESTER,
    Action.WRITE_TESTS: AppRole.LEAD,
    Action.APPROVE_WRITES: AppRole.LEAD,
    Action.SVN_WRITE_TOOLS: AppRole.LEAD,
    Action.MANAGE_ENVIRONMENTS: AppRole.ADMIN,
    Action.MANAGE_ROLES: AppRole.ADMIN,
}

DESCRIPTION = {
    AppRole.VIEWER: "Look at saved results, history and reports. Cannot connect to Polarion.",
    AppRole.TESTER: "Viewer, plus connect and run every read-only test (health, data, requirements, scripts).",
    AppRole.LEAD: "Tester, plus run behavioural tests that create test data, clean it up, and approve test projects and accounts, and start TortoiseSVN checkout/merge.",
    AppRole.ADMIN: "Everything, including production environments, deleting environments and the role passphrase.",
}


class RoleService:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    # ------------------------------------------------------------------ storage
    def _get(self, key: str) -> str | None:
        rows = self.db.query("SELECT value FROM app_settings WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def _put(self, key: str, value: str | None) -> None:
        with self.db.tx() as c:
            if value is None:
                c.execute("DELETE FROM app_settings WHERE key=?", (key,))
            else:
                c.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------------ role
    @property
    def current(self) -> AppRole:
        raw = self._get("app_role")
        try:
            return AppRole(raw) if raw else AppRole.ADMIN        # first run: the person who installed the tool
        except ValueError:
            return AppRole.VIEWER                                # a damaged value fails closed

    @property
    def passphrase_set(self) -> bool:
        return self._get("role_passphrase") is not None

    def allows(self, action: Action) -> bool:
        return self.current.rank >= MINIMUM[action].rank

    def require(self, action: Action) -> None:
        if not self.allows(action):
            need = MINIMUM[action]
            self.audit.record("role.denied", "denied", target=action.name, detail={"role": self.current.value})
            raise RoleDenied(f"Your role ({self.current.label}) cannot {action.value}. "
                             f"This needs the {need.label} role or higher.")

    def set_role(self, role: AppRole, passphrase: str = "") -> AppRole:
        before = self.current
        if role.rank > before.rank and self.passphrase_set and not self._check(passphrase):
            self.audit.record("role.change", "denied", target=role.value, detail={"from": before.value})
            raise RoleDenied("The role passphrase is wrong.")
        self._put("app_role", role.value)
        self.audit.record("role.change", "ok", target=role.value, detail={"from": before.value})
        return role

    # ------------------------------------------------------------------ passphrase
    def set_passphrase(self, new: str, old: str = "") -> None:
        """Set, change (`old` required) or clear (`new` empty) the passphrase that guards raising the role."""
        self.require(Action.MANAGE_ROLES)
        if self.passphrase_set and not self._check(old):
            raise RoleDenied("The current role passphrase is wrong.")
        if not new:
            self._put("role_passphrase", None)
        else:
            if len(new) < 6:
                raise RoleDenied("Use at least 6 characters for the role passphrase.")
            salt = os.urandom(16)
            self._put("role_passphrase", f"{salt.hex()}${_hash(new, salt).hex()}")
        self.audit.record("role.passphrase", "ok", detail={"set": bool(new)})

    def _check(self, passphrase: str) -> bool:
        stored = self._get("role_passphrase")
        if not stored:
            return True
        salt_hex, digest_hex = stored.split("$", 1)
        return hmac.compare_digest(_hash(passphrase, bytes.fromhex(salt_hex)).hex(), digest_hex)


def _hash(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 200_000)
