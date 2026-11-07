"""Polarion release identifiers.

Since Polarion 2304 releases are named YYMM and the internal version is 3.YY.M
(2410 = 3.24.10, 2506 = 3.25.6, 2512 = 3.25.12, 2606 = 3.26.6). The mapping is generic,
so a future release needs no code change here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_CODE = re.compile(r"^(\d{2})(\d{2})$")
_INTERNAL = re.compile(r"^3\.(\d{2})\.(\d{1,2})(?:\.\d+)*$")


@total_ordering
@dataclass(frozen=True)
class Release:
    year: int    # two digits, e.g. 26
    month: int   # 1..12

    def __post_init__(self) -> None:
        if not (1 <= self.month <= 12) or not (20 <= self.year <= 99):
            raise ValueError(f"Not a Polarion release: {self.year:02d}{self.month:02d}")

    @classmethod
    def parse(cls, text: str) -> "Release":
        """Accepts '2606', 'Polarion 2606', '3.26.6' or '3.26.6.1'."""
        t = text.strip()
        m = _INTERNAL.match(t)
        if m:
            return cls(int(m.group(1)), int(m.group(2)))
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", t)
        if m and _CODE.match(m.group(1)):
            return cls(int(m.group(1)[:2]), int(m.group(1)[2:]))
        raise ValueError(f"Not a Polarion release: {text!r}")

    @classmethod
    def try_parse(cls, text: str | None) -> "Release | None":
        if not text:
            return None
        try:
            return cls.parse(text)
        except ValueError:
            return None

    @property
    def code(self) -> str:
        return f"{self.year:02d}{self.month:02d}"

    @property
    def internal(self) -> str:
        return f"3.{self.year:02d}.{self.month}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Release):
            return NotImplemented
        return (self.year, self.month) < (other.year, other.month)

    def __str__(self) -> str:
        return self.code
