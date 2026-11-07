"""Export a test run to Excel, CSV, HTML or PDF. All data comes from executed checks; secrets never appear."""
from __future__ import annotations

import csv
import html
from pathlib import Path

from .. import APP_NAME, __version__
from ..domain.testing import TestRunRecord
from ..infra.redaction import scrub

STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "partial_pass": "PARTIAL PASS", "blocked": "BLOCKED",
                "not_testable": "NOT TESTABLE", "error": "ERROR"}
STATUS_COLOR = {"pass": "C6EFCE", "fail": "FFC7CE", "partial_pass": "FFEB9C", "blocked": "DDEBF7",
                "not_testable": "EDEDED", "error": "FFC7CE"}


def _header(run: TestRunRecord) -> list[tuple[str, str]]:
    c = run.counts()
    return [
        ("Application", f"{APP_NAME} {__version__}"), ("Server", run.server_url), ("Environment", run.environment_name),
        ("Polarion release", run.release or "unknown"), ("Adapter", run.adapter), ("Project", run.project_id),
        ("Run type", run.kind), ("Requirement set", run.requirement_set or "-"),
        ("Started", run.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
        ("Finished", run.finished_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if run.finished_at else "-"),
        ("Run by", run.user), ("Verdict", run.verdict),
        ("Checks", f"{run.total}: {c['pass']} pass, {c['fail']} fail, {c['partial_pass']} partial, "
                   f"{c['blocked']} blocked, {c['not_testable']} not testable, {c['error']} error"),
        ("Coverage (checks with a verdict)", f"{run.coverage_pct}%"),
    ]


FORMULA_START = ("=", "+", "-", "@", chr(9), chr(13))


def _s(value) -> str:
    """Spreadsheet cell text: secrets scrubbed, and a leading = + - @ neutralised so Excel never runs it as a formula
    (Work Item titles and other server text end up in these cells)."""
    text = scrub(str(value)) or ""
    return "'" + text if text[:1] in FORMULA_START else text


def export_csv(run: TestRunRecord, path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Group", "Check", "Name", "Status", "Requirement", "Criterion", "Expected", "Actual",
                    "Mechanism", "Evidence"])
        for r in run.results:
            w.writerow([_s(x) for x in (r.group, r.check_id, r.name, STATUS_LABEL[r.status.value], r.requirement_label,
                                        r.criterion, r.expected, r.actual, r.mechanism, " | ".join(r.evidence))])
    return path


def export_xlsx(run: TestRunRecord, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    for i, (k, v) in enumerate(_header(run), 1):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, _s(v))
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 90
    row = len(_header(run)) + 2
    if run.inspection_gaps:
        ws.cell(row, 1, "Could not be inspected").font = Font(bold=True)
        for g in run.inspection_gaps:
            row += 1
            ws.cell(row, 2, _s(g)).alignment = Alignment(wrap_text=True)
    res = wb.create_sheet("Results")
    heads = ["Group", "Check", "Name", "Status", "Requirement", "Criterion", "Expected", "Actual", "Mechanism", "Evidence"]
    res.append(heads)
    for c in range(1, len(heads) + 1):
        res.cell(1, c).font = Font(bold=True, color="FFFFFF")
        res.cell(1, c).fill = PatternFill("solid", fgColor="14213D")
    for r in run.results:
        res.append([_s(x) for x in (r.group, r.check_id, r.name, STATUS_LABEL[r.status.value], r.requirement_label, r.criterion,
                                    r.expected, r.actual, r.mechanism, "\n".join(r.evidence))])
        cell = res.cell(res.max_row, 4)
        cell.fill = PatternFill("solid", fgColor=STATUS_COLOR[r.status.value])
        for c in range(1, len(heads) + 1):
            res.cell(res.max_row, c).alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([14, 10, 44, 14, 16, 40, 44, 44, 26, 70], 1):
        res.column_dimensions[get_column_letter(i)].width = w
    res.freeze_panes = "A2"
    res.auto_filter.ref = res.dimensions
    wb.save(path)
    return Path(path)


def export_html(run: TestRunRecord, path: str | Path) -> Path:
    e = lambda s: html.escape(scrub(str(s)) or "")  # noqa: E731
    colors = {"pass": "#e7f6ec", "fail": "#fdeaea", "partial_pass": "#fff6e0", "blocked": "#eaf1ff",
              "not_testable": "#f0f1f4", "error": "#fdeaea"}
    head = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in _header(run))
    gaps = ("<h2>Could not be inspected</h2><ul>" + "".join(f"<li>{e(g)}</li>" for g in run.inspection_gaps) + "</ul>") \
        if run.inspection_gaps else ""
    rows = "".join(
        f"<tr style='background:{colors[r.status.value]}'><td>{e(r.group)}</td><td>{e(r.check_id)}</td><td>{e(r.name)}</td>"
        f"<td><b>{STATUS_LABEL[r.status.value]}</b></td><td>{e(r.requirement_label)}</td><td>{e(r.expected)}</td>"
        f"<td>{e(r.actual)}</td><td>{e(r.mechanism)}</td><td>{'<br>'.join(e(x) for x in r.evidence)}</td></tr>"
        for r in run.results)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Test report - {e(run.project_id)}</title>
<style>body{{font:14px Segoe UI,Arial;margin:24px;color:#1d2433}}h1{{margin-bottom:4px}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #dde2ec;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#eef1f7}}
.head th{{width:260px}}</style></head><body>
<h1>{e(APP_NAME)}: test report</h1><p>Project <b>{e(run.project_id)}</b> on {e(run.server_url)}</p>
<table class="head">{head}</table>{gaps}
<h2>Results</h2><table><tr><th>Group</th><th>Check</th><th>Name</th><th>Status</th><th>Requirement</th><th>Expected</th>
<th>Actual</th><th>Mechanism</th><th>Evidence</th></tr>{rows}</table>
<p style="color:#6b7487">Generated by {e(APP_NAME)} {e(__version__)}. No AI was used. Every result comes from an executed check.</p>
</body></html>"""
    Path(path).write_text(doc, encoding="utf-8")
    return Path(path)


def export_pdf(run: TestRunRecord, path: str | Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def p(text, style):
        return Paragraph(html.escape(scrub(str(text)) or "").replace(chr(10), "<br/>"), style)

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7, leading=8.5)
    head_style = styles["BodyText"].clone("head", fontSize=8, leading=10)
    fills = {"pass": "#e7f6ec", "fail": "#fdeaea", "partial_pass": "#fff6e0", "blocked": "#eaf1ff",
             "not_testable": "#f0f1f4", "error": "#fdeaea"}
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=f"{APP_NAME} test report", author=APP_NAME)
    story = [Paragraph(html.escape(f"{APP_NAME}: test report"), styles["Title"])]
    meta = Table([[p(k, head_style), p(v, head_style)] for k, v in _header(run)], colWidths=[60 * mm, 200 * mm])
    meta.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dde2ec")),
                              ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef1f7")),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [meta, Spacer(1, 6 * mm)]
    if run.inspection_gaps:
        story.append(Paragraph("Could not be inspected", styles["Heading2"]))
        story += [p("- " + g, head_style) for g in run.inspection_gaps]
        story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Results", styles["Heading2"]))
    rows = [[p(h, small) for h in ("Group", "Check", "Name", "Status", "Requirement", "Expected", "Actual", "Evidence")]]
    style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dde2ec")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14213d")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    for i, r in enumerate(run.results, start=1):
        rows.append([p(r.group, small), p(r.check_id, small), p(r.name, small), p(STATUS_LABEL[r.status.value], small),
                     p(r.requirement_label, small), p(r.expected, small), p(r.actual, small),
                     p(chr(10).join(r.evidence[:6]), small)])
        style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(fills[r.status.value])))
    table = Table(rows, colWidths=[20 * mm, 12 * mm, 48 * mm, 20 * mm, 24 * mm, 50 * mm, 40 * mm, 46 * mm], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 4 * mm),
              p(f"Generated by {APP_NAME} {__version__}. No AI was used. Every result comes from an executed check.", small)]
    doc.build(story)
    return Path(path)


EXPORTERS = {"xlsx": export_xlsx, "csv": export_csv, "html": export_html, "pdf": export_pdf}


def export_run(run: TestRunRecord, path: str | Path) -> Path:
    fmt = Path(path).suffix.lower().lstrip(".")
    if fmt not in EXPORTERS:
        raise ValueError(f"Unsupported export format '{fmt}'. Use: {', '.join(EXPORTERS)}")
    return EXPORTERS[fmt](run, path)
