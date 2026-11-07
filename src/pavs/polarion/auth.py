"""Authentication manager: which credential opens which provider."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.enums import AuthMethod, ProviderKind
from ..domain.errors import CredentialError
from ..domain.models import Credentials, Environment


_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*$")


def looks_like_jwt(token: str | None) -> bool:
    """A JSON Web Token (e.g. issued by the company's single sign-on) has three dot-separated base64url parts."""
    return bool(token and _JWT.match(token.strip()))


@dataclass(frozen=True)
class AuthPlan:
    rest_token: str | None
    soap_token: str | None
    soap_user: str | None
    soap_password: str | None
    svn_user: str | None
    svn_password: str | None
    rest_token_is_jwt: bool = False

    def can_authenticate(self, provider: ProviderKind) -> bool:
        if provider == ProviderKind.REST:
            return bool(self.rest_token)
        if provider == ProviderKind.SOAP:
            return bool(self.soap_token or (self.soap_user and self.soap_password))
        if provider == ProviderKind.SVN:
            return bool(self.svn_user and self.svn_password)
        return False

    def explain(self, provider: ProviderKind) -> str:
        if self.can_authenticate(provider):
            return ""
        return {
            ProviderKind.REST: "REST needs a Personal Access Token (Polarion REST has no password login).",
            ProviderKind.SOAP: ("A JWT works for REST only: SOAP needs a Personal Access Token or a username and password."
                                if self.rest_token_is_jwt else
                                "SOAP needs a Personal Access Token or a username and password."),
            ProviderKind.SVN: "The SVN configuration reader needs a username and password.",
        }.get(provider, "No credential for this provider.")


def plan_auth(env: Environment, creds: Credentials | None) -> AuthPlan:
    method = env.auth_method
    creds = creds or Credentials()
    if method == AuthMethod.NONE:
        return AuthPlan(None, None, None, None, None, None)
    token = creds.token if method in (AuthMethod.TOKEN, AuthMethod.TOKEN_AND_PASSWORD) else None
    pw = creds.password if method in (AuthMethod.PASSWORD, AuthMethod.TOKEN_AND_PASSWORD) else None
    if method in (AuthMethod.TOKEN, AuthMethod.TOKEN_AND_PASSWORD) and not token:
        raise CredentialError("This environment uses a Personal Access Token but none is stored.")
    if method in (AuthMethod.PASSWORD, AuthMethod.TOKEN_AND_PASSWORD):
        if not pw:
            raise CredentialError("This environment uses a password but none is stored.")
        if not env.username:
            raise CredentialError("Password authentication needs a username.")
    user = env.username if pw else None
    svn_user = creds.svn_user or env.username
    svn_password = creds.svn_password or pw
    is_jwt = looks_like_jwt(token)
    return AuthPlan(
        rest_token=token,
        rest_token_is_jwt=is_jwt,
        soap_token=None if is_jwt else token,       # SOAP logInWithToken takes Personal Access Tokens, not JWTs
        soap_user=user,
        soap_password=pw,
        svn_user=svn_user,
        svn_password=svn_password,
    )
