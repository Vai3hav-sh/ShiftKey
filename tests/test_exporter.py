import io
from datetime import date

import openpyxl

from app.exporter import build_csv, build_workbook, export_filename, workbook_bytes
from app.parser import parse_report


def _records(raw_bytes):
    rep = parse_report(raw_bytes, "daily format.xlsx")
    return rep, [r.to_dict() for r in rep.records]


def test_workbook_matches_hand_made_layout(raw_bytes, formatted_bytes):
    rep, recs = _records(raw_bytes)
    wb = build_workbook(report_date=rep.report_date, company=rep.company, title=rep.title,
                        records=recs, highlight=False, summary=False)
    ws = openpyxl.load_workbook(io.BytesIO(workbook_bytes(wb))).worksheets[0]
    target = openpyxl.load_workbook(io.BytesIO(formatted_bytes)).worksheets[0]

    assert ws["C2"].value == "Company Name:OMI" and ws["C2"].font.b
    assert ws["C3"].value == "DAILY PERFORMANCE REPORT FOR DATE : 22/10/2026"
    assert [c.value for c in ws[5][1:7]] == [c.value.strip() for c in target[5][1:7]]
    assert all(ws.cell(row=5, column=c).border.left.style == "medium" for c in range(2, 8))
    assert ws["A6"].value.startswith("** Department Code & Name : 001  A.O.D")
    assert ws["B7"].value == "100284" and ws["C7"].value == "MUKESH KUMAR PANDEY"
    assert ws["D7"].number_format == "h:mm" and ws["D7"].value.strftime("%H:%M") == "06:47"
    assert ws["G7"].font.b and ws["G7"].value == "12.14"
    assert round(ws.column_dimensions["C"].width, 1) == 24.3

    emp_rows = [r for r in ws.iter_rows(min_row=6) if r[1].value]
    assert len(emp_rows) == 172
    dept_rows = [r for r in ws.iter_rows(min_row=6) if r[0].value]
    assert len(dept_rows) == 30

    mis_cells = [r[5] for r in emp_rows if (r[5].value or "").strip() == "MIS"]
    assert len(mis_cells) == 2 and all(c.font.b and c.font.color.rgb == "FFFF0000" for c in mis_cells)
    hlf_cells = [r[5] for r in emp_rows if (r[5].value or "").strip() == "HLF"]
    assert len(hlf_cells) == 3 and all(c.font.b for c in hlf_cells)


def test_workbook_with_summary_sheets(raw_bytes):
    rep, recs = _records(raw_bytes)
    wb = build_workbook(report_date=rep.report_date, company=rep.company, title=rep.title,
                        records=recs, highlight=True, summary=True, run_at=rep.run_at)
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes(wb)))
    assert wb.sheetnames == ["Daily Report", "Half Days", "Mispunches", "Needs Review", "Absent", "Summary"]
    hd = wb["Half Days"]
    assert hd["A2"].value == "3 employee(s)"
    assert hd["E5"].value in {"Pooja Chaudhary", "SURAJ KUMAR", "TEJAS PRAJAPATI"}
    assert hd["D5"].value == "Indian"
    main = wb["Daily Report"]
    shaded = [r for r in main.iter_rows(min_row=6) if r[1].value and r[1].fill.fill_type == "solid"]
    assert len(shaded) == 3 + 2 + 4  # halfday + mispunch + long-span warnings


def test_csv_and_filename(raw_bytes):
    rep, recs = _records(raw_bytes)
    body = build_csv(report_date=rep.report_date, records=recs).decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert len(lines) == 173
    assert lines[0].startswith("Date,Dept Code,Department,PayCode")
    assert "2026-10-22,001,A.O.D,100284,Indian" in lines[1]
    assert export_filename(date(2026, 10, 21), "Expatriates", "xlsx") == "21 October 2026 Expatriates.xlsx"
    assert export_filename(None, "", "csv") == "Undated.csv"
