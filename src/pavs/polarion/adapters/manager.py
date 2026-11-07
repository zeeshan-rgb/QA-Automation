"""Chooses the adapter for a detected release."""
from __future__ import annotations

from ..version.release import Release
from .base import VersionAdapter
from .releases import RELEASE_ADAPTERS, FutureAdapter


class AdapterManager:
    def __init__(self, adapters: tuple[type[VersionAdapter], ...] = RELEASE_ADAPTERS,
                 validated: set[str] | None = None) -> None:
        self._classes = sorted(adapters, key=lambda c: c().release)  # type: ignore[arg-type]
        self._validated = validated or set()

    @property
    def known_releases(self) -> list[Release]:
        return [c().release for c in self._classes]  # type: ignore[misc]

    def select(self, release: Release | None) -> tuple[VersionAdapter, list[str]]:
        """Exact match, else nearest lower, else FutureAdapter (newer) / oldest (older)."""
        notes: list[str] = []
        if release is None:
            adapter: VersionAdapter = FutureAdapter(release=None, name="Unknown release")
            notes.append("Release unknown: using newest behaviour with live probing only.")
            return self._mark(adapter), notes
        exact = [c for c in self._classes if c().release == release]
        if exact:
            return self._mark(exact[0]()), notes
        lower = [c for c in self._classes if c().release < release]  # type: ignore[operator]
        newest = self._classes[-1]().release
        if release > newest:  # type: ignore[operator]
            adapter = FutureAdapter(release=release)
            notes.append(f"No adapter for {release.code}; using newest behaviour ({newest.code}) + probing.")
            return self._mark(adapter), notes
        if lower:
            adapter = lower[-1]()
            notes.append(f"No adapter for {release.code} (not a known Polarion release); "
                         f"using the nearest lower adapter {adapter.name}.")
            return self._mark(adapter), notes
        adapter = self._classes[0]()
        notes.append(f"{release.code} is older than every supported release; "
                     f"using {adapter.name}. Results may be unreliable.")
        return self._mark(adapter), notes

    def _mark(self, adapter: VersionAdapter) -> VersionAdapter:
        if adapter.release is not None and adapter.release.code in self._validated:
            adapter.validated = True
        return adapter
