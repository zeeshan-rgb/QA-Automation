"""Exception hierarchy. Messages must never contain secrets."""


class PavsError(Exception):
    """Base class for all application errors."""


class ConfigurationError(PavsError):
    """Invalid environment or application configuration."""


class CredentialError(PavsError):
    """Credential missing, unreadable, or no secure storage available."""


class TransportError(PavsError):
    """Network, TLS or timeout problem talking to a server."""


class WriteNotAuthorized(PavsError):
    """A non-read request was attempted without a valid Write Guard authorization."""


class PolarionApiError(PavsError):
    """Polarion answered with an error status."""

    def __init__(self, status: int, title: str = "", detail: str = "", source: str = "") -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.source = source
        super().__init__(f"HTTP {status} {title}: {detail}".strip())


class AuthenticationFailed(PolarionApiError):
    pass


class PermissionDenied(PolarionApiError):
    pass


class NotFound(PolarionApiError):
    pass


class SoapFault(PavsError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"SOAP fault {code}: {message}")
