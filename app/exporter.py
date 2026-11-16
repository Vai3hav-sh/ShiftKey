"""Write the formatted daily report (the hand-made "Expatriates" layout) as xlsx / csv."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import SEGMENT_LABELS


def _seg(r):
    return r.get("segment_label") or SEGMENT_LABELS.get(r.get("segment") or "", "")

# A record here is a plain dict (works for both ParsedRecord.to_dict() and ORM rows).
Record = Dict[str, Any]

FONT = "Calibri"
BASE = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
RED_BOLD = Font(name=FONT, size=10, bold=True, color="FFFF0000")
AMBER_BOLD = Font(name=FONT, size=10, bold=True, color="FFC65911")
GREY = Font(name=FONT, size=10, color="FF7F7F7F")
MEDIUM = Side(style="medium")
BOX = Border(left=MEDIUM, right=MEDIUM, top=MEDIUM, bottom=MEDIUM)
CENTER = Alignment(horizontal="center")
FILL_MIS = PatternFill("solid", fgColor="FFFDE2E1")
FILL_HLF = PatternFill("solid", fgColor="FFFFF1CC")
FILL_WARN = PatternFill("solid", fgColor="FFEDE7F6")
FILL_HEAD = PatternFill("solid", fgColor="FFF2F2F2")
FILL_FIXED = PatternFill("solid", fgColor="FFE3F5E6")
GREEN_BOLD = Font(name=FONT, size=10, bold=True, color="FF2B8A3E")


def _t(hhmm: Optional[str]) -> Optional[time]:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def report_date_label(d: Optional[date]) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else "Undated"


def export_filename(d: Optional[date], suffix: str, ext: str) -> str:
    base = report_date_label(d)
    return f"{base} {suffix}.{ext}".strip() if suffix else f"{base}.{ext}"


def group_by_department(records: Iterable[Record]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    index: Dict[tuple, Dict[str, Any]] = {}
    for r in records:
        key = (r.get("dept_code"), r.get("dept_name"))
        g = index.get(key)
        if g is None:
            g = {"code": r.get("dept_code"), "name": r.get("dept_name"), "records": []}
            index[key] = g
            groups.append(g)
        g["records"].append(r)
    return groups


def build_workbook(*, report_date: Optional[date], company: Optional[str], title: Optional[str],
                   records: List[Record], highlight: bool = True, summary: bool = True,
                   run_at: Optional[str] = None) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Report"
    _write_main_sheet(ws, report_date, company, title, records, highlight)
    if summary:
        flagged = [r for r in records if r.get("is_halfday")]
        _write_list_sheet(wb.create_sheet("Half Days"), "HALF DAYS", report_date, flagged)
        flagged = [r for r in records if r.get("is_mispunch")]
        _write_list_sheet(wb.create_sheet("Mispunches"), "MISPUNCHES", report_date, flagged)
        flagged = [r for r in records if r.get("has_warning") and not r.get("is_mispunch")]
        if flagged:
            _write_list_sheet(wb.create_sheet("Needs Review"), "NEEDS REVIEW", report_date, flagged)
        flagged = [r for r in records if r.get("is_absent")]
        _write_list_sheet(wb.create_sheet("Absent"), "ABSENT", report_date, flagged)
        fixed = [r for r in records if r.get("is_corrected")]
        if fixed:
            _write_corrections_sheet(wb.create_sheet("Corrections"), report_date, fixed)
        _write_summary_sheet(wb.create_sheet("Summary"), report_date, company, records, run_at)
    return wb


def _write_main_sheet(ws, report_date, company, title, records, highlight):
    d = report_date.strftime("%d/%m/%Y") if report_date else ""
    ws["C2"] = f"Company Name:{company or ''}"
    ws["C2"].font = BOLD
    ws["C3"] = f"{title or 'DAILY PERFORMANCE REPORT'} FOR DATE : {d}"
    ws["C3"].font = BOLD

    headers = ["PayCode", "Employee Name", "In", "Out", "Status", "Hrs Works"]
    for i, h in enumerate(headers, start=2):
        c = ws.cell(row=5, column=i, value=h)
        c.font = BOLD
        c.border = BOX
        if i >= 4:
            c.alignment = CENTER
    ws.row_dimensions[4].height = 13.5
    ws.row_dimensions[5].height = 13.5

    row = 6
    for g in group_by_department(records):
        code = g["code"] or ""
        name = g["name"] or ""
        c = ws.cell(row=row, column=1, value=f"** Department Code & Name : {code:<4} {name}")
        c.font = BASE
        row += 1
        for r in g["records"]:
            status = (r.get("status") or "").strip()
            cells = [
                ws.cell(row=row, column=2, value=r.get("paycode")),
                ws.cell(row=row, column=3, value=r.get("name")),
                ws.cell(row=row, column=4, value=_t(r.get("time_in"))),
                ws.cell(row=row, column=5, value=_t(r.get("time_out"))),
                ws.cell(row=row, column=6, value=status),
                ws.cell(row=row, column=7, value=r.get("hrs") or ""),
            ]
            for c in cells:
                c.font = BASE
            cells[2].number_format = cells[3].number_format = "h:mm"
            for c in cells[2:]:
                c.alignment = CENTER
            cells[5].font = BOLD
            if r.get("is_mispunch"):
                cells[4].font = RED_BOLD
            elif r.get("is_halfday"):
                cells[4].font = AMBER_BOLD
            elif r.get("is_corrected"):
                cells[4].font = GREEN_BOLD
            if highlight:
                fill = None
                if r.get("is_corrected"):
                    fill = FILL_FIXED
                elif r.get("is_mispunch"):
                    fill = FILL_MIS
                elif r.get("is_halfday"):
                    fill = FILL_HLF
                elif r.get("has_warning"):
                    fill = FILL_WARN
                if fill is not None:
                    for c in cells:
                        c.fill = fill
            row += 1

    ws.column_dimensions["A"].width = 9.14
    ws.column_dimensions["B"].width = 10.5
    ws.column_dimensions["C"].width = 24.29
    for col in "DEFG":
        ws.column_dimensions[col].width = 9.14
    ws.freeze_panes = "A6"
    ws.sheet_view.showGridLines = True
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "5:5"


def _write_list_sheet(ws, heading: str, report_date: Optional[date], records: List[Record]):
    ws["A1"] = f"{heading} – {report_date_label(report_date)}"
    ws["A1"].font = Font(name=FONT, size=12, bold=True)
    ws["A2"] = f"{len(records)} employee(s)"
    ws["A2"].font = GREY
    headers = ["#", "Department", "PayCode", "Segment", "Employee Name", "Shift", "In", "Out", "Hrs Works", "Status", "Remarks"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=i, value=h)
        c.font = BOLD
        c.fill = FILL_HEAD
        c.border = Border(bottom=Side(style="thin"))
    row = 5
    for n, r in enumerate(records, start=1):
        remarks = list(r.get("notes") or [])
        if r.get("is_corrected"):
            c = r.get("correction") or {}
            remarks.insert(0, f"Resolved by {c.get('by') or '?'}" + (f": {c.get('remark')}" if c.get("remark") else ""))
        vals = [n, r.get("dept_name"), r.get("paycode"), _seg(r), r.get("name"), r.get("shift"),
                _t(r.get("time_in")), _t(r.get("time_out")), r.get("hrs") or "",
                (r.get("status") or "").strip(), "; ".join(remarks)]
        for i, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=i, value=v)
            c.font = BASE
            if i in (7, 8):
                c.number_format = "h:mm"
                c.alignment = CENTER
            if i in (4, 6, 9, 10):
                c.alignment = CENTER
        row += 1
    widths = [4, 22, 10, 8, 26, 7, 7, 7, 10, 8, 60]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A5"


def _write_corrections_sheet(ws, report_date: Optional[date], records: List[Record]):
    ws["A1"] = f"CORRECTIONS – {report_date_label(report_date)}"
    ws["A1"].font = Font(name=FONT, size=12, bold=True)
    ws["A2"] = f"{len(records)} record(s) resolved"
    ws["A2"].font = GREY
    headers = ["Department", "PayCode", "Employee Name", "Shift", "Original In", "Original Out", "Original Status",
               "Corrected In", "Corrected Out", "Corrected Status", "Hrs", "Resolved By", "Resolved At", "Remark"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=i, value=h)
        c.font = BOLD
        c.fill = FILL_HEAD
        c.border = Border(bottom=Side(style="thin"))
    row = 5
    for r in records:
        o = r.get("original") or {}
        c = r.get("correction") or {}
        at = (c.get("at") or "").replace("T", " ")[:16]
        vals = [r.get("dept_name"), r.get("paycode"), r.get("name"), r.get("shift"),
                _t(o.get("time_in")), _t(o.get("time_out")), o.get("status"),
                _t(r.get("time_in")), _t(r.get("time_out")), r.get("status"), r.get("hrs") or "",
                c.get("by"), at, c.get("remark")]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=row, column=i, value=v)
            cell.font = BASE
            if i in (5, 6, 8, 9):
                cell.number_format = "h:mm"
                cell.alignment = CENTER
        row += 1
    for i, w in enumerate([22, 10, 26, 7, 11, 12, 14, 12, 13, 15, 7, 18, 17, 40], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A5"


def _write_summary_sheet(ws, report_date, company, records: List[Record], run_at):
    ws["A1"] = f"ATTENDANCE SUMMARY – {report_date_label(report_date)}"
    ws["A1"].font = Font(name=FONT, size=12, bold=True)
    ws["A2"] = f"Company: {company or ''}"
    ws["A2"].font = GREY
    if run_at:
        ws["A3"] = f"Machine run time: {run_at}"
        ws["A3"].font = GREY
    ws["A4"] = f"Generated: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ws["A4"].font = GREY

    totals = {
        "Employees": len(records),
        "Present": sum(1 for r in records if r.get("is_present")),
        "Absent": sum(1 for r in records if r.get("is_absent")),
        "Half days": sum(1 for r in records if r.get("is_halfday")),
        "Mispunches": sum(1 for r in records if r.get("is_mispunch")),
        "Needs review": sum(1 for r in records if r.get("has_warning")),
        "Pending resolution": sum(1 for r in records if r.get("needs_action")),
        "Resolved": sum(1 for r in records if r.get("is_corrected")),
        "Night shift": sum(1 for r in records if r.get("is_night")),
        "Indian staff": sum(1 for r in records if r.get("segment") == "indian"),
        "Local staff": sum(1 for r in records if r.get("segment") == "local"),
    }
    row = 6
    for k, v in totals.items():
        ws.cell(row=row, column=1, value=k).font = BOLD
        ws.cell(row=row, column=2, value=v).font = BASE
        row += 1

    row += 1
    headers = ["Code", "Department", "Employees", "Present", "Absent", "Half days", "Mispunches", "Needs review"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = BOLD
        c.fill = FILL_HEAD
        c.border = Border(bottom=Side(style="thin"))
    row += 1
    for g in group_by_department(records):
        rs = g["records"]
        vals = [g["code"], g["name"], len(rs),
                sum(1 for r in rs if r.get("is_present")),
                sum(1 for r in rs if r.get("is_absent")),
                sum(1 for r in rs if r.get("is_halfday")),
                sum(1 for r in rs if r.get("is_mispunch")),
                sum(1 for r in rs if r.get("has_warning"))]
        for i, v in enumerate(vals, start=1):
            ws.cell(row=row, column=i, value=v).font = BASE
        row += 1
    for col, w in zip("ABCDEFGH", [8, 28, 11, 9, 9, 10, 11, 12]):
        ws.column_dimensions[col].width = w


def workbook_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_csv(*, report_date: Optional[date], records: List[Record]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Dept Code", "Department", "PayCode", "Segment", "Card No", "Employee Name", "Shift",
                "Shift Start", "In", "Lunch Out", "Lunch In", "Out", "Hrs Works", "Status",
                "Status Label", "Early", "Late", "Left Early", "OT", "Manual", "Half Day",
                "Mispunch", "Night", "Needs Review", "Corrected", "Original In", "Original Out",
                "Original Status", "Resolved By", "Correction Remark", "Remarks"])
    d = report_date.isoformat() if report_date else ""
    for r in records:
        o = r.get("original") or {}
        c = r.get("correction") or {}
        w.writerow([
            d, r.get("dept_code"), r.get("dept_name"), r.get("paycode"), _seg(r), r.get("card_no"),
            r.get("name"), r.get("shift"), r.get("shift_start"), r.get("time_in"),
            r.get("lunch_out"), r.get("lunch_in"), r.get("time_out"), r.get("hrs"),
            (r.get("status") or "").strip(), r.get("status_label"), r.get("early"), r.get("late"),
            r.get("shift_early"), r.get("ot"), "Y" if r.get("manual") else "",
            "Y" if r.get("is_halfday") else "", "Y" if r.get("is_mispunch") else "",
            "Y" if r.get("is_night") else "", "Y" if r.get("has_warning") else "",
            "Y" if r.get("is_corrected") else "", o.get("time_in"), o.get("time_out"), o.get("status"),
            c.get("by"), c.get("remark"), "; ".join(r.get("notes") or []),
        ])
    return buf.getvalue().encode("utf-8-sig")
