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

    def save(self, key: str, creds: Credentials) -> None:
        if not self._backend.secure:
            raise CredentialError("No secure credential storage is available on this system.")
        payload = json.dumps({"token": creds.token, "password": creds.password})
        try:
            self._backend.set(key, payload)
        except Exception as exc:
            raise CredentialError(f"Could not store credentials: {type(exc).__name__}") from None
        SecretRegistry.register(creds.token, creds.password)

    def load(self, key: str) -> Credentials | None:
        try:
            raw = self._backend.get(key)
        except Exception as exc:
            raise CredentialError(f"Could not read credentials: {type(exc).__name__}") from None
        if not raw:
            return None
        data = json.loads(raw)
        creds = Credentials(token=data.get("token"), password=data.get("password"))
        SecretRegistry.register(creds.token, creds.password)
        return creds

    def delete(self, key: str) -> None:
        self._backend.delete(key)
