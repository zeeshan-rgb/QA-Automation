"""Enumerations shared by every layer."""
from enum import Enum


class EnvironmentKind(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthMethod(str, Enum):
    NONE = "none"                      # anonymous: version detection only
    TOKEN = "token"                    # Personal Access Token (REST bearer, SOAP logInWithToken)
    PASSWORD = "password"              # username + password (SOAP logIn, SVN basic)
    TOKEN_AND_PASSWORD = "token_and_password"


class ProviderKind(str, Enum):
    REST = "rest"
    SOAP = "soap"
    SVN = "svn"
    BROWSER = "browser"
    DATABASE = "database"


class ProviderState(str, Enum):
    NOT_CONFIGURED = "not_configured"  # user did not enable it for this environment
    UNREACHABLE = "unreachable"        # network/TLS error
    DISABLED = "disabled"              # server reachable but the channel is switched off
    AVAILABLE = "available"            # channel present, not authenticated (yet)
    AUTHENTICATED = "authenticated"
    AUTH_FAILED = "auth_failed"


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    NEEDS_PERMISSION = "needs_permission"
    UNKNOWN = "unknown"


class Verification(str, Enum):
    """How we know a capability status. Ordered from weakest to strongest."""
    DOCUMENTED = "documented"   # SDK / spec / change log says so
    DETECTED = "detected"       # live spec or route probe on this server
    VERIFIED = "verified"       # a real call returned the expected data, with evidence

    @property
    def rank(self) -> int:
        return ["documented", "detected", "verified"].index(self.value)


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL_PASS = "partial_pass"
    BLOCKED = "blocked"
    NOT_TESTABLE = "not_testable"
    ERROR = "error"             # the tool itself failed


class TestOrigin(str, Enum):
    RULE_GENERATED = "rule_generated"
    MANUAL = "manual"
