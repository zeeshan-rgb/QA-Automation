"""Importers and the rule engine (no AI: fixed patterns, deterministic)."""
import json

import pytest

from pavs.domain.requirements import CheckKind, Expectation, FindingKind
from pavs.engines.requirements.importers import (ImportError_, import_csv, import_file, import_text,
                                                  split_criteria)
from pavs.engines.requirements.rules import find_conflicts, generate_for_requirement

EXAMPLE = """REQ-1: Create a new Polarion role called Internal Reviewer. The role should have permission to review Work Items but should not have permission to modify or delete them.
Acceptance criteria:
- The role Internal Reviewer exists in the project
- Internal Reviewer can comment on Work Items
- Internal Reviewer cannot modify Work Items
"""


def checks(text):
    reqs = import_text(text).requirements
    out = []
    for r in reqs:
        g = generate_for_requirement(r)
        out += [(c.check.kind, c.check.expect, c.check.target) for c in g.cases]
    return out


# ---------------------------------------------------------------- importers
def test_text_ids_criteria_and_gwt():
    reqs = import_text(EXAMPLE + "\nREQ-2: Given a draft\nWhen it is reviewed\nThen the status is Reviewed\n").requirements
    assert [r.external_id for r in reqs] == ["REQ-1", "REQ-2"]
    assert len(reqs[0].acceptance_criteria) == 3 and "Internal Reviewer exists" in reqs[0].acceptance_criteria[0]
    assert len(reqs[1].acceptance_criteria) == 1 and reqs[1].acceptance_criteria[0].startswith("Given a draft When")


def test_text_numbered_and_paragraphs():
    assert len(import_text("1. Users can export.\n2. Admins can delete.\n").requirements) == 2
    assert len(import_text("First thing must happen.\n\nSecond thing must happen.").requirements) == 2
    assert import_text("   \n").requirements == []


def test_split_criteria():
    body, crit = split_criteria("Do it.\nAC1: first\nAC2: second")
    assert body == "Do it." and crit == ["first", "second"]


def test_csv_semicolon_and_aliases():
    data = "Req ID;Title;Description;Acceptance Criteria;Owner\nR1;Role;The role Reviewer exists.;Role exists; bob\n".encode()
    res = import_csv(data, "x.csv")
    r = res.requirements[0]
    assert r.external_id == "R1" and r.text == "The role Reviewer exists." and r.acceptance_criteria == ["Role exists"]
    assert r.attributes == {"Owner": "bob"}


def test_csv_without_header_warns():
    res = import_csv(b"foo,bar\n1,2\n", "x.csv")
    assert not res.requirements and "no header" in res.warnings[0]


def test_xlsx_docx_pdf_json_yaml(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Requirement", "Acceptance Criteria"])
    ws.append(["R1", "The field Reviewer is mandatory for Requirement work items.", "Saving without Reviewer is rejected"])
    wb.save(tmp_path / "r.xlsx")
    assert import_file(tmp_path / "r.xlsx").requirements[0].external_id == "R1"

    import docx
    d = docx.Document()
    d.add_paragraph("REQ-9: The role Auditor exists.")
    d.save(tmp_path / "r.docx")
    assert import_file(tmp_path / "r.docx").requirements[0].external_id == "REQ-9"

    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(tmp_path / "r.pdf"))
    c.drawString(72, 750, "REQ-5: The Priority enumeration must contain the values High and Low.")
    c.save()
    assert import_file(tmp_path / "r.pdf").requirements[0].external_id == "REQ-5"

    spec = {"requirements": [{"id": "S1", "text": "Reviewer is mandatory.", "acceptance_criteria": ["a"],
                              "tests": [{"title": "t", "check": {"kind": "role_exists", "expect": "present",
                                                                 "target": {"role": "x"}}},
                                        {"title": "bad", "check": {"kind": "nope"}}]}]}
    (tmp_path / "s.json").write_text(json.dumps(spec))
    res = import_file(tmp_path / "s.json")
    assert len(res.requirements) == 1 and len(res.test_cases) == 1 and "invalid" in res.warnings[0]
    assert res.test_cases[0].origin.value == "manual"
    (tmp_path / "s.yaml").write_text("- id: Y1\n  text: The role X exists.\n")
    assert import_file(tmp_path / "s.yaml").requirements[0].external_id == "Y1"


def test_bad_input_is_rejected_cleanly(tmp_path):
    (tmp_path / "a.exe").write_text("x")
    with pytest.raises(ImportError_, match="Unsupported"):
        import_file(tmp_path / "a.exe")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(ImportError_, match="invalid JSON"):
        import_file(tmp_path / "bad.json")
    (tmp_path / "bad.xlsx").write_text("not a workbook")
    with pytest.raises(ImportError_, match="Excel"):
        import_file(tmp_path / "bad.xlsx")


# ---------------------------------------------------------------- rules
def test_internal_reviewer_example_from_the_brief():
    got = checks(EXAMPLE)
    perms = {(t["permission"].rsplit(".", 2)[-2] + "." + t["permission"].rsplit(".", 1)[-1], e.value)
             for k, e, t in got if k == CheckKind.PERMISSION}
    assert perms == {("WorkItem.read", "granted"), ("Comment.create", "granted"),
                     ("WorkItem.modify", "denied"), ("WorkItem.delete", "denied")}
    assert (CheckKind.ROLE_EXISTS, Expectation.PRESENT, {"role": "Internal Reviewer"}) in got
    assert all(t["role"] == "Internal Reviewer" for k, e, t in got if k == CheckKind.PERMISSION)
    assert len(got) == 5                                  # criteria that restate the text add no duplicates


@pytest.mark.parametrize("text,kind,expect,target", [
    ("The field Reviewer is mandatory for Requirement work items.", CheckKind.FIELD_REQUIRED, Expectation.REQUIRED,
     {"field": "Reviewer", "type": "Requirement"}),
    ("Saving a Requirement without Reviewer is rejected.", CheckKind.FIELD_REQUIRED, Expectation.REQUIRED,
     {"field": "Reviewer", "type": "Requirement"}),
    ("Requirement work items can move from Draft to Reviewed.", CheckKind.TRANSITION, Expectation.ALLOWED,
     {"from_status": "Draft", "to_status": "Reviewed"}),
    ("Requirement work items cannot move from Approved to Draft.", CheckKind.TRANSITION, Expectation.BLOCKED,
     {"from_status": "Approved", "to_status": "Draft"}),
    ("The review action requires the field Reviewer for Requirement work items.", CheckKind.ACTION_REQUIRES_FIELD,
     Expectation.REQUIRED, {"action": "review", "field": "Reviewer"}),
    ("The Priority enumeration must contain the values High, Low and Medium.", CheckKind.ENUM_CONTAINS,
     Expectation.PRESENT, {"enum": "Priority", "options": ["High", "Low", "Medium"]}),
    ("Define a work item type called Risk.", CheckKind.TYPE_EXISTS, Expectation.PRESENT, {"type": "Risk"}),
    ("Create a custom field called Safety Class of type Enumeration for Requirement work items.", CheckKind.FIELD_EXISTS,
     Expectation.PRESENT, {"field": "Safety Class", "type": "Requirement", "field_type": "enumeration"}),
    ("Every Requirement must be linked to a Test Case.", CheckKind.TRACEABILITY, Expectation.COVERED,
     {"from_type": "Requirement", "to_type": "Test Case"}),
    ("Add a document type called Specification.", CheckKind.DOCUMENT_TYPE_EXISTS, Expectation.PRESENT,
     {"document_type": "Specification"}),
])
def test_rule_patterns(text, kind, expect, target):
    got = checks(text)
    assert len(got) == 1, got
    k, e, t = got[0]
    assert k == kind and e == expect
    for key, value in target.items():
        assert t[key] == value, (key, t)


def test_unknown_and_vague_sentences_are_reported_not_guessed():
    r = import_text("REQ-1: The system shall be fast.\nREQ-2: The planets must align on Tuesdays.").requirements
    g1 = generate_for_requirement(r[0])
    assert not g1.cases and any(f.kind == FindingKind.AMBIGUOUS for f in g1.findings)
    g2 = generate_for_requirement(r[1])
    assert not g2.cases and any(f.kind == FindingKind.NOT_AUTOMATABLE for f in g2.findings)
    assert any(f.kind == FindingKind.MISSING_CRITERIA for f in g2.findings)


def test_conflicting_requirements_are_found():
    reqs = import_text("A1: The role Tester can modify Work Items.\nA2: The role Tester cannot modify Work Items.").requirements
    per = {r.id: generate_for_requirement(r).cases for r in reqs}
    found = find_conflicts(per, {r.id: r.label for r in reqs})
    assert found and found[0].kind == FindingKind.CONFLICT
