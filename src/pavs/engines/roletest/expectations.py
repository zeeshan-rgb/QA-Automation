"""The expected permissions per role, read from the customer's Excel sheet (one block of columns per object type).

Layout (as in the "Requirement Management ALM Roles" workbook): a row whose first cell is ROLE holds the action names;
the row above holds the object name over the first column of each block; the rows below list one role each with
True / False / empty. Empty means "not granted".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ...domain.errors import ConfigurationError


class Expect(str, Enum):
    YES = "yes"            # the role must be able to do it
    NO = "no"              # the sheet says No: it must be refused
    BLANK = "blank"        # the sheet does not grant it: it should be refused
    INFO = "info"          # no statement in the sheet: report what happens, never a blocker


def key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _cell(v) -> Expect:
    s = str(v).strip().lower() if v is not None else ""
    if s in ("true", "yes", "y", "1", "x", "allowed"):
        return Expect.YES
    if s in ("false", "no", "n", "0", "denied"):
        return Expect.NO
    return Expect.BLANK


@dataclass
class Matrix:
    roles: dict[str, dict[str, dict[str, Expect]]] = field(default_factory=dict)   # role -> group key -> action key -> Expect
    group_names: dict[str, str] = field(default_factory=dict)                      # group key -> name as written in the sheet
    sheet: str = ""
    source: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def match_group(self, type_id: str, type_name: str) -> str | None:
        """The sheet's name for a Work Item type: exact name, then id, then a name that ends with the sheet's word (REQUEST)."""
        names = {key(type_name), key(type_id)}
        for k in names:
            if k in self.group_names:
                return k
        ends = [k for k in self.group_names if k and any(n.endswith(k) for n in names if n)]
        return ends[0] if len(ends) == 1 else None

    def expect(self, roles: list[str], group_key: str | None, action: str) -> Expect:
        if group_key is None:
            return Expect.BLANK if self.roles else Expect.INFO
        found = [self.roles[r].get(group_key, {}).get(key(action)) for r in roles if r in self.roles]
        found = [f for f in found if f is not None]
        if not found:
            return Expect.BLANK
        if Expect.YES in found:
            return Expect.YES
        return Expect.NO if Expect.NO in found else Expect.BLANK


def sheet_names(path: str | Path) -> list[str]:
    wb = _open(path)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _open(path):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:                                     # pragma: no cover
        raise ConfigurationError("Reading Excel needs the openpyxl package.") from exc
    try:
        return load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:
        raise ConfigurationError(f"The Excel file could not be read: {type(exc).__name__}") from None


def load_matrix(path: str | Path, sheet: str | None = None) -> Matrix:
    wb = _open(path)
    try:
        name = sheet or wb.sheetnames[0]
        if name not in wb.sheetnames:
            raise ConfigurationError(f"Sheet '{name}' is not in the workbook.")
        rows = [list(r) for r in wb[name].iter_rows(values_only=True)]
    finally:
        wb.close()
    m = Matrix(sheet=name, source=str(path))
    i = 0
    while i < len(rows):
        if str(rows[i][0] or "").strip().upper() != "ROLE":
            i += 1
            continue
        header, groups = rows[i], (rows[i - 1] if i else [])
        cols: list[tuple[int, str, str]] = []                      # column, group key, action key
        group = ""
        for c in range(1, len(header)):
            g = str(groups[c]).strip() if c < len(groups) and groups[c] else ""
            if g:
                group = g
            action = str(header[c] or "").strip()
            if action and group and action.upper() != "SOURCE":
                cols.append((c, key(group), key(action)))
                m.group_names.setdefault(key(group), group)
        j = i + 1
        while j < len(rows) and str(rows[j][0] or "").strip():
            role = str(rows[j][0]).strip()
            per = m.roles.setdefault(role, {})
            for c, g, a in cols:
                per.setdefault(g, {})[a] = _cell(rows[j][c] if c < len(rows[j]) else None)
            j += 1
        i = j
    if not m.roles:
        raise ConfigurationError("No permission table was found: the sheet needs a row starting with ROLE above the role rows.")
    return m
