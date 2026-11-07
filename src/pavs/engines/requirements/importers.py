"""Requirement importers: plain text, CSV, Excel, Word, PDF, JSON/YAML (incl. structured test specs).

Input files are untrusted: size-limited, parsed with data-only readers, never executed.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ...domain.enums import TestOrigin
from ...domain.errors import PavsError
from ...domain.requirements import (CheckSpec, Polarity, Requirement, RequirementSet, TestCase,
                                    TestCaseStatus)

MAX_BYTES = 20 * 1024 * 1024
MAX_REQUIREMENTS = 5000

ID_LINE = re.compile(r"^\s*(?P<id>(?:[A-Z][A-Z0-9_]*-\d+(?:\.\d+)*)|(?:REQ[ _]?\d+)|(?:(?!AC\s?\d)[A-Za-z]{1,8}\d{1,5}(?:\.\d+)*(?=\s*:)))\s*[:.)\-–]?\s+(?P<rest>.*)$")
NUMBERED = re.compile(r"^\s*(?P<n>\d{1,4})[.)]\s+(?P<rest>.+)$")
BULLET = re.compile(r"^\s*(?:[-*•▪●]|\(?[a-z]\)|\d+\.\d*)\s+(?P<rest>.+)$")
AC_HEADER = re.compile(r"^\s*(acceptance\s+criteria|acceptance|criteria|ac)\s*[:\-]?\s*(?P<rest>.*)$", re.I)
AC_LINE = re.compile(r"^\s*AC\s*\d+\s*[:.)\-]\s*(?P<rest>.+)$", re.I)
GWT = re.compile(r"^\s*(given|when|then|and|but)\b", re.I)

COLUMN_ALIASES = {
    "external_id": {"id", "key", "req id", "requirement id", "reference", "ref", "#", "req", "number", "no", "item id"},
    "title": {"title", "name", "summary", "headline", "short description", "subject"},
    "text": {"description", "text", "requirement", "details", "statement", "requirement text", "body"},
    "acceptance": {"acceptance criteria", "acceptance", "ac", "criteria", "expected result", "expected",
                   "acceptance criterion"},
}


class ImportError_(PavsError):
    """Raised for unreadable or unsupported input files."""


@dataclass
class ImportResult:
    requirement_set: RequirementSet
    requirements: list[Requirement] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)      # only from structured specifications
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- text segmentation
def _title_of(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    return first[:120].rstrip(" .")


def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def split_criteria(text: str) -> tuple[str, list[str]]:
    """Separate explicit acceptance criteria (AC header + bullets, 'AC1:' lines, Given/When/Then) from the body."""
    body, criteria = [], []
    in_ac = False
    in_gwt = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = AC_LINE.match(line)
        if m:
            criteria.append(_clean(m.group("rest")))
            in_ac = True
            continue
        m = AC_HEADER.match(line)
        if m and (line.strip().lower().startswith(("acceptance", "criteria")) or line.strip()[:3].lower() == "ac:"):
            in_ac = True
            if m.group("rest").strip():
                criteria.append(_clean(m.group("rest")))
            continue
        g = GWT.match(line)
        if g:
            # Given/When/Then/And lines form ONE scenario criterion
            if in_gwt and g.group(1).lower() != "given":
                criteria[-1] = f"{criteria[-1]} {_clean(line)}"
            else:
                criteria.append(_clean(line))
            in_gwt = True
            continue
        in_gwt = False
        if in_ac:
            b = BULLET.match(line)
            criteria.append(_clean(b.group("rest") if b else line))
            continue
        body.append(line.strip())
    return _clean(" ".join(body)), criteria


def segment_text(text: str, source: str) -> list[Requirement]:
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    blocks: list[tuple[str, list[str], str]] = []          # (external id, lines, location)
    if any(ID_LINE.match(l) for l in lines):
        cur: tuple[str, list[str], str] | None = None
        for i, l in enumerate(lines, 1):
            m = ID_LINE.match(l)
            if m:
                if cur:
                    blocks.append(cur)
                cur = (m.group("id"), [m.group("rest")], f"line {i}")
            elif cur:
                cur[1].append(l)
        if cur:
            blocks.append(cur)
    elif sum(bool(NUMBERED.match(l)) for l in lines) >= 2:
        cur = None
        for i, l in enumerate(lines, 1):
            m = NUMBERED.match(l)
            if m and not (cur and BULLET.match(l) and cur[1] and AC_HEADER.match(cur[1][-1] or "")):
                if cur:
                    blocks.append(cur)
                cur = ("", [m.group("rest")], f"line {i}")
            elif cur:
                cur[1].append(l)
        if cur:
            blocks.append(cur)
    else:
        para: list[str] = []
        start = 1
        for i, l in enumerate(lines + [""], 1):
            if l.strip():
                if not para:
                    start = i
                para.append(l)
            elif para:
                blocks.append(("", para, f"line {start}"))
                para = []
    out = []
    for ext, blines, loc in blocks:
        body, crit = split_criteria("\n".join(blines))
        if not body and not crit:
            continue
        out.append(Requirement(external_id=ext, title=_title_of(body or crit[0]), text=body or " ".join(crit),
                               acceptance_criteria=crit, source=f"{source}, {loc}"))
    return out


# ---------------------------------------------------------------- tables (CSV / Excel / Word tables)
def _match_columns(header: list[str]) -> dict[str, int] | None:
    norm = [re.sub(r"[^a-z0-9# ]", "", (h or "").strip().lower()) for h in header]
    found: dict[str, int] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for i, h in enumerate(norm):
            if h in aliases and i not in found.values():
                found[key] = i
                break
    if "text" not in found and "title" in found:
        found["text"] = found["title"]
    return found if "text" in found else None


def rows_to_requirements(rows: list[list[str]], source: str) -> tuple[list[Requirement], list[str]]:
    warnings = []
    header_idx, cols = None, None
    for i, row in enumerate(rows[:20]):
        if sum(bool((c or "").strip()) for c in row) >= 1:
            cols = _match_columns([str(c or "") for c in row])
            if cols:
                header_idx = i
                break
    if cols is None:
        return [], [f"{source}: no header row with a requirement/description column was found"]
    header = [str(c or "").strip() for c in rows[header_idx]]
    out = []
    for r, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        cells = [str(c).strip() if c is not None else "" for c in row]
        get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""  # noqa: E731
        text = get("text")
        if not text and not get("title"):
            continue
        ac_cell = get("acceptance")
        _, embedded = split_criteria(text) if "\n" in text else ("", [])
        crit = [c for c in (re.split(r"\n|;\s*", ac_cell) if ac_cell else [])]
        crit = [_clean(BULLET.match(c).group("rest") if BULLET.match(c) else c) for c in crit if c.strip()]
        body = split_criteria(text)[0] if embedded else _clean(text)
        attrs = {header[i]: cells[i] for i in range(min(len(header), len(cells)))
                 if i not in cols.values() and header[i] and cells[i]}
        out.append(Requirement(external_id=get("external_id"), title=get("title") or _title_of(body),
                               text=body or get("title"), acceptance_criteria=crit + embedded,
                               source=f"{source}, row {r}", attributes=attrs))
    if not out:
        warnings.append(f"{source}: header found but no requirement rows")
    return out, warnings


# ---------------------------------------------------------------- formats
def _read_bytes(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ImportError_(f"{path.name} is {size // 1024 // 1024} MB; the limit is {MAX_BYTES // 1024 // 1024} MB.")
    return path.read_bytes()


def import_text(text: str, name: str = "pasted text") -> ImportResult:
    rs = RequirementSet(name=name, source_format="text")
    reqs = segment_text(text, name)
    return ImportResult(rs, reqs, warnings=[] if reqs else ["No requirements found in the text."])


def import_csv(data: bytes, name: str) -> ImportResult:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    reqs, warns = rows_to_requirements(rows, name)
    return ImportResult(RequirementSet(name=name, source_file=name, source_format="csv"), reqs, warnings=warns)


def import_xlsx(data: bytes, name: str) -> ImportResult:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ImportError_(f"{name}: not a readable Excel workbook ({type(exc).__name__})") from None
    reqs, warns = [], []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        got, w = rows_to_requirements(rows, f"{name} [{ws.title}]")
        reqs += got
        warns += [x for x in w if got or len(wb.worksheets) == 1]
    wb.close()
    return ImportResult(RequirementSet(name=name, source_file=name, source_format="xlsx"), reqs, warnings=warns)


def import_docx(data: bytes, name: str) -> ImportResult:
    import docx
    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ImportError_(f"{name}: not a readable Word document ({type(exc).__name__})") from None
    reqs, warns = [], []
    for ti, table in enumerate(doc.tables, 1):
        rows = [[c.text for c in row.cells] for row in table.rows]
        got, _ = rows_to_requirements(rows, f"{name} table {ti}")
        reqs += got
    lines = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            lines.append("")
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        lines.append(("- " + t) if "list" in style and not ID_LINE.match(t) else t)
    reqs += segment_text("\n".join(lines), name)
    return ImportResult(RequirementSet(name=name, source_file=name, source_format="docx"), reqs, warnings=warns)


def import_pdf(data: bytes, name: str) -> ImportResult:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages]
    except Exception as exc:
        raise ImportError_(f"{name}: not a readable PDF ({type(exc).__name__})") from None
    text = "\n".join(pages)
    warns = [] if text.strip() else [f"{name}: no text layer (scanned PDF?). OCR is not supported."]
    return ImportResult(RequirementSet(name=name, source_file=name, source_format="pdf"),
                        segment_text(text, name), warnings=warns)


def import_structured(obj, name: str, fmt: str) -> ImportResult:
    """JSON/YAML: a list of requirements, {"requirements": [...]}, optionally with "tests" per requirement."""
    rs = RequirementSet(name=name, source_file=name, source_format=fmt)
    items = obj.get("requirements") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise ImportError_(f"{name}: expected a list of requirements or an object with a 'requirements' list")
    res = ImportResult(rs)
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            res.warnings.append(f"{name}: item {i} is not an object - skipped")
            continue
        text = str(it.get("text") or it.get("description") or it.get("requirement") or "").strip()
        title = str(it.get("title") or it.get("name") or "").strip()
        ac = it.get("acceptance_criteria") or it.get("acceptanceCriteria") or it.get("criteria") or []
        ac = [ac] if isinstance(ac, str) else [str(a) for a in ac]
        if not (text or title):
            res.warnings.append(f"{name}: item {i} has no text - skipped")
            continue
        req = Requirement(external_id=str(it.get("id") or it.get("external_id") or ""), title=title or _title_of(text),
                          text=text or title, acceptance_criteria=ac, source=f"{name}, item {i}",
                          attributes={k: str(v) for k, v in it.items() if isinstance(v, (str, int, float)) and k not in
                                      ("id", "external_id", "text", "description", "requirement", "title", "name")})
        res.requirements.append(req)
        for j, t in enumerate(it.get("tests") or [], 1):
            try:
                check = CheckSpec.model_validate(t.get("check") or {})
                res.test_cases.append(TestCase(
                    requirement_id=req.id, title=str(t.get("title") or check.describe()),
                    criterion=str(t.get("criterion") or ""), polarity=Polarity(t.get("polarity", "positive")),
                    check=check, origin=TestOrigin.MANUAL, status=TestCaseStatus.PROPOSED,
                    rationale="from structured test specification"))
            except (ValidationError, ValueError, AttributeError) as exc:
                res.warnings.append(f"{name}: item {i} test {j} is invalid: {str(exc).splitlines()[0]}")
    return res


IMPORTERS = {".txt": "text", ".md": "text", ".csv": "csv", ".tsv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx",
             ".docx": "docx", ".pdf": "pdf", ".json": "json", ".yaml": "yaml", ".yml": "yaml"}


def import_file(path: str | Path) -> ImportResult:
    path = Path(path)
    kind = IMPORTERS.get(path.suffix.lower())
    if kind is None:
        raise ImportError_(f"Unsupported file type '{path.suffix}'. Supported: {', '.join(sorted(IMPORTERS))}")
    data = _read_bytes(path)
    if kind == "text":
        res = import_text(data.decode("utf-8-sig", errors="replace"), path.name)
        res.requirement_set.source_file = path.name
    elif kind == "csv":
        res = import_csv(data, path.name)
    elif kind == "xlsx":
        res = import_xlsx(data, path.name)
    elif kind == "docx":
        res = import_docx(data, path.name)
    elif kind == "pdf":
        res = import_pdf(data, path.name)
    elif kind == "json":
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ImportError_(f"{path.name}: invalid JSON ({exc})") from None
        res = import_structured(obj, path.name, "json")
    else:
        import yaml
        try:
            obj = yaml.safe_load(data.decode("utf-8-sig"))
        except yaml.YAMLError as exc:
            raise ImportError_(f"{path.name}: invalid YAML ({str(exc).splitlines()[0]})") from None
        res = import_structured(obj, path.name, "yaml")
    if len(res.requirements) > MAX_REQUIREMENTS:
        res.warnings.append(f"Only the first {MAX_REQUIREMENTS} of {len(res.requirements)} requirements were kept.")
        res.requirements = res.requirements[:MAX_REQUIREMENTS]
    for r in res.requirements:
        r.set_id = res.requirement_set.id
    if not res.requirements:
        res.warnings.append("No requirements were found.")
    return res
