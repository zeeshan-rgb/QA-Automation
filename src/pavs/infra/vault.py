"""Credential vault.

Secrets live in the OS credential store (Windows Credential Manager via `keyring`,
DPAPI-protected per Windows user). The database only keeps the environment id.
If no secure backend exists, nothing is stored: the UI must ask every session.
"""
from __future__ import annotations

import json
from typing import Protocol

from ..domain.errors import CredentialError
from ..domain.models import Credentials
from .redaction import SecretRegistry

SERVICE_NAME = "PolarionValidationStudio"


class VaultBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    @property
    def secure(self) -> bool: ...


class KeyringBackend:
    def __init__(self) -> None:
        import keyring
        from keyring.backends import fail

        self._kr = keyring
        backend = keyring.get_keyring()
        self._secure = not isinstance(backend, fail.Keyring) and "plaintext" not in type(backend).__name__.lower()

    @property
    def secure(self) -> bool:
        return self._secure

    def get(self, key: str) -> str | None:
        return self._kr.get_password(SERVICE_NAME, key)

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(SERVICE_NAME, key, value)

    def delete(self, key: str) -> None:
        import keyring.errors
        try:
            self._kr.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryBackend:
    """Session-only storage (tests, or when no secure OS store exists)."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    @property
    def secure(self) -> bool:
        return True   # never leaves process memory

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class CredentialVault:
    def __init__(self, backend: VaultBackend | None = None) -> None:
        if backend is None:
            try:
                kr = KeyringBackend()
                backend = kr if kr.secure else MemoryBackend()
            except Exception:
                backend = MemoryBackend()
        self._backend = backend

    @property
    def persistent(self) -> bool:
        return not isinstance(self._backend, MemoryBackend)

    # Windows Credential Manager refuses entries above about 1,250 characters (verified on Windows 11 with `keyring`: 1,000 stored,
    # 1,500 failed). A JWT alone can be longer, so a longer payload is split over several entries (key, key#0, key#1, ...).
    CHUNK = 600
    SINGLE_MAX = 900
    MARKER = "PAVS-CHUNKS:"

    def _put(self, key: str, payload: str) -> None:
        self._drop_chunks(key)
        if len(payload) <= self.SINGLE_MAX:
            self._backend.set(key, payload)
            return
        parts = [payload[i:i + self.CHUNK] for i in range(0, len(payload), self.CHUNK)]
        try:
            for i, part in enumerate(parts):
                self._backend.set(f"{key}#{i}", part)
            self._backend.set(key, f"{self.MARKER}{len(parts)}")
        except Exception:
            self._drop_chunks(key, len(parts))
            raise

    def _get(self, key: str) -> str | None:
        raw = self._backend.get(key)
        if raw and raw.startswith(self.MARKER):
            n = int(raw[len(self.MARKER):])
            parts = [self._backend.get(f"{key}#{i}") for i in range(n)]
            if any(p is None for p in parts):
                raise CredentialError("The stored credentials are incomplete. Enter them again.")
            return "".join(parts)
        return raw

    def _drop_chunks(self, key: str, count: int | None = None) -> None:
        if count is None:
            raw = self._backend.get(key)
            count = int(raw[len(self.MARKER):]) if raw and raw.startswith(self.MARKER) else 0
        for i in range(count):
            self._backend.delete(f"{key}#{i}")

    def save(self, key: str, creds: Credentials) -> None:
        if not self._backend.secure:
            raise CredentialError("No secure credential storage is available on this system.")
        payload = json.dumps({"token": creds.token, "password": creds.password,
                              "svn_user": creds.svn_user, "svn_password": creds.svn_password})
        try:
            self._put(key, payload)
        except Exception as exc:
            raise CredentialError(f"Could not store credentials: {type(exc).__name__}") from None
        SecretRegistry.register(creds.token, creds.password, creds.svn_password)

    def load(self, key: str) -> Credentials | None:
        try:
            raw = self._get(key)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"Could not read credentials: {type(exc).__name__}") from None
        if not raw:
            return None
        data = json.loads(raw)
        creds = Credentials(token=data.get("token"), password=data.get("password"),
                            svn_user=data.get("svn_user"), svn_password=data.get("svn_password"))
        SecretRegistry.register(creds.token, creds.password, creds.svn_password)
        return creds

    def delete(self, key: str) -> None:
        try:
            self._drop_chunks(key)
        except Exception:
            pass
        self._backend.delete(key)
