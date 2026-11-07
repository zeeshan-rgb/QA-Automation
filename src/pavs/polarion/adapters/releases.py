"""Release adapters. Only real Polarion releases are registered (2410, 2506, 2512, 2606).

The SDK REST change log and the Polarion blog list no 2503 or 2603 release. A server that
reports an unregistered release is handled by nearest-lower selection + live probing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..version.release import Release
from .base import Quirk, VersionAdapter

Q_HOOK_TYPE_SCOPE = Quirk(
    "save-hook-type-scope",
    "Save Hooks cannot be scoped to a Work Item type before 2506; scripts must filter by type themselves.",
    ("scripts.source",), "Polarion 2410 client experience")
Q_NO_REST_CUSTOMFIELDS = Quirk(
    "no-rest-customfields",
    "REST custom-field configuration endpoints arrive in 2512; older releases use SOAP or SVN.",
    ("customfields.definitions",), "SDK REST change log 3.25.12")
Q_NO_REST_METADATA = Quirk(
    "no-rest-metadata",
    "REST /metadata arrives in 2512; the version comes from unauthenticated signals.",
    ("server.metadata",), "SDK REST change log 3.25.12")
Q_NO_DOC_LISTING = Quirk(
    "no-rest-document-listing",
    "REST Document and Page listing arrive in 2512; older releases list Documents via SOAP queryModules.",
    ("documents.list", "pages.list"), "SDK REST change log 3.25.12")
Q_NO_BACKLINKS = Quirk(
    "no-rest-backlinks",
    "REST back-links endpoint arrives in 2512; older releases use SOAP getBackLinkedWorkitems.",
    ("workitems.backlinks",), "SDK REST change log 3.25.12")
Q_JAKARTA = Quirk(
    "tomcat11-jakarta",
    "2606 runs on Tomcat 11 / Jakarta EE 11 and removed Groovy; server-side extensions must be rebuilt.",
    ("scripts.source",), "Polarion blog 2026-05-14")


@dataclass
class Adapter2410(VersionAdapter):
    release: Release = field(default_factory=lambda: Release.parse("2410"))
    name: str = "Polarion 2410"
    quirks: list = field(default_factory=lambda: [
        Q_HOOK_TYPE_SCOPE, Q_NO_REST_CUSTOMFIELDS, Q_NO_REST_METADATA, Q_NO_DOC_LISTING, Q_NO_BACKLINKS])

    def custom_fields_via_rest(self) -> bool:
        return False

    def save_hooks_scoped_by_type(self) -> bool:
        return False


@dataclass
class Adapter2506(VersionAdapter):
    release: Release = field(default_factory=lambda: Release.parse("2506"))
    name: str = "Polarion 2506"
    quirks: list = field(default_factory=lambda: [
        Q_NO_REST_CUSTOMFIELDS, Q_NO_REST_METADATA, Q_NO_DOC_LISTING, Q_NO_BACKLINKS])

    def custom_fields_via_rest(self) -> bool:
        return False


@dataclass
class Adapter2512(VersionAdapter):
    release: Release = field(default_factory=lambda: Release.parse("2512"))
    name: str = "Polarion 2512"


@dataclass
class Adapter2606(VersionAdapter):
    release: Release = field(default_factory=lambda: Release.parse("2606"))
    name: str = "Polarion 2606"
    quirks: list = field(default_factory=lambda: [Q_JAKARTA])


@dataclass
class FutureAdapter(VersionAdapter):
    """Newest known behaviour for releases newer than any registered adapter."""
    name: str = "Future release"

    def warnings(self) -> list[str]:
        label = self.release.code if self.release else "an unknown release"
        return [f"Polarion {label} is newer than every validated adapter. Capabilities come from live "
                "probing only; run the compatibility suite before relying on results."]


RELEASE_ADAPTERS: tuple[type[VersionAdapter], ...] = (Adapter2410, Adapter2506, Adapter2512, Adapter2606)
