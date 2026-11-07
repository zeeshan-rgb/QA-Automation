"""Remove secrets from headers, bodies, URLs and log text.

Used by the HTTP transport (evidence) AND the logging handler, so a secret that slips
past one layer is still caught by the other.
"""
from __future__ import annotations

import re
from typing import Mapping

REDACTED = "***REDACTED***"

SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "x-polarion-rest-token", "cookie",
    "set-cookie", "x-csrf-token",
}

# SOAP/XML elements whose text content is secret
_XML_SECRET = re.compile(
    r"(<(?:[\w-]+:)?(password|token|sessionID)\b[^>]*>)(.*?)(</(?:[\w-]+:)?\2>)",
    re.IGNORECASE | re.DOTALL,
)
# JSON "password": "...", "token": "..."
_JSON_SECRET = re.compile(
    r'("(?:password|passwd|token|accessToken|access_token|apiKey|api_key|secret)"\s*:\s*")([^"]*)(")',
    re.IGNORECASE,
)
# Bearer / Basic credentials anywhere in free text
_AUTH_IN_TEXT = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
# key=value in query strings or text
_KV_SECRET = re.compile(
    r"\b(password|passwd|token|access_token|api_key|apikey|secret)=([^&\s\"']+)", re.IGNORECASE)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: (REDACTED if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def redact_text(text: str | None, extra_secrets: tuple[str, ...] = ()) -> str | None:
    if text is None:
        return None
    out = _XML_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(4)}", text)
    out = _JSON_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", out)
    out = _AUTH_IN_TEXT.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _KV_SECRET.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, REDACTED)
    return out


class SecretRegistry:
    """Process-wide set of known secret values, so they are scrubbed even in odd places."""

    _secrets: set[str] = set()

    @classmethod
    def register(cls, *values: str | None) -> None:
        for v in values:
            if v and len(v) >= 4:
                cls._secrets.add(v)

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._secrets, key=len, reverse=True))

    @classmethod
    def clear(cls) -> None:
        cls._secrets.clear()


def scrub(text: str | None) -> str | None:
    return redact_text(text, SecretRegistry.values())
