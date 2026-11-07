"""Security hardening: untrusted XML, secrets in exports, spreadsheet formula injection."""
import pytest

from pavs.domain.enums import TestStatus as S
from pavs.domain.testing import CheckResult, TestRunRecord
from pavs.infra import safexml
from pavs.infra.redaction import SecretRegistry
from pavs.reporting import exporters

LAUGHS = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>'


def test_dtd_and_entities_are_refused():
    for doc in (LAUGHS, b'<!DOCTYPE x SYSTEM "file:///c:/windows/win.ini"><x/>', b"<!ENTITY e 'x'><x/>"):
        with pytest.raises(safexml.ParseError):
            safexml.fromstring(doc)
    assert safexml.fromstring("<a><b>1</b></a>").find("b").text == "1"


def test_hostile_xml_from_the_repository_is_reported_not_expanded():
    from pavs.engines.inspection.parsers import ConfigParseError, parse_projects
    with pytest.raises(ConfigParseError):
        parse_projects(LAUGHS.decode())


def test_secrets_are_scrubbed_from_every_export_format(tmp_path):
    SecretRegistry.register("hunter-22-secret", None)
    run = TestRunRecord(environment_id="e", project_id="p", user="u")
    run.results = [CheckResult(check_id="X1", group="G", name="n", status=S.FAIL, actual="token hunter-22-secret leaked",
                               evidence=["Authorization: Bearer hunter-22-secret"])]
    for ext in ("csv", "xlsx", "html", "pdf"):
        out = exporters.export_run(run, tmp_path / f"r.{ext}")
        data = out.read_bytes()
        if ext == "xlsx":
            from openpyxl import load_workbook
            data = " ".join(str(c.value) for ws in load_workbook(out) for row in ws.iter_rows() for c in row).encode()
        assert b"hunter-22-secret" not in data, ext


def test_spreadsheet_cells_cannot_start_a_formula(tmp_path):
    run = TestRunRecord(environment_id="e", project_id="p", user="u")
    run.results = [CheckResult(check_id="X1", group="G", name='=HYPERLINK("http://evil","x")', status=S.PASS,
                               actual="@SUM(1)", evidence=["+cmd|' /C calc'!A0"])]
    from openpyxl import load_workbook
    ws = load_workbook(exporters.export_run(run, tmp_path / "r.xlsx"))["Results"]
    cells = [str(c.value) for c in ws[2]]
    assert not any(v.startswith(("=", "+", "-", "@")) for v in cells)
    assert "'=HYPERLINK" in " ".join(cells)
    csv_text = exporters.export_run(run, tmp_path / "r.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in csv_text
