"""Mock Polarion server (WSGI, no dependencies) with release profiles for tests and UI work."""
from .app import MockPolarion
from .profiles import PROFILES, MockProfile

__all__ = ["MockPolarion", "PROFILES", "MockProfile"]
