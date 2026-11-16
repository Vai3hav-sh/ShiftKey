"""Parse a Starlink "Daily Performance Report" export into structured records.

The Starlink Time Office software (v12.x) exports a sheet that looks like:

    Company Name:OMI
    Run Date & Time :23/10/2026 16:22
    DAILY PERFORMANCE REPORT FOR DATE : 22/10/2026
    Sr.No. | PayCode | Card No | Employee Name | Shift | Start | In | ... | Status | ...
    ** Department Code & Name : 001  A.O.D
    1 | 100284 | 00100284 | MUKESH KUMAR PANDEY | M07 | 07:00 | 06:47 | ... | P | ...

Columns are located by header name, so both the full 25-column export and the
trimmed 6-column "formatted" sheet parse the same way.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import settings

# ----------------------------------------------------------------------------
# Column recognition
# ----------------------------------------------------------------------------

# normalised header text -> canonical field name
HEADER_ALIASES: Dict[str, str] = {
    "sr.no.": "seq", "sr no": "seq", "srno": "seq", "sr.no": "seq", "sno": "seq", "s.no.": "seq",
    "paycode": "paycode", "pay code": "paycode", "emp code": "paycode", "empcode": "paycode",
    "card no": "card_no", "cardno": "card_no", "card no.": "card_no",
    "employee name": "name", "name": "name", "emp name": "name",
    "shift": "shift",
    "start": "shift_start", "shift start": "shift_start",
    "in": "time_in", "in time": "time_in", "intime": "time_in",
    "lunchout": "lunch_out", "lunch out": "lunch_out",
    "lunchin": "lunch_in", "lunch in": "lunch_in",
    "out": "time_out", "out time": "time_out", "outtime": "time_out",
    "hrs works": "hrs", "hrs work": "hrs", "work hrs": "hrs", "hours worked": "hrs", "hrs": "hrs",
    "status": "status",
    "early arriv.": "early", "early arriv": "early", "early arrival": "early",
    "late arriv.": "late", "late arriv": "late", "late arrival": "late",
    "shift early": "shift_early", "early dep.": "shift_early", "early departure": "shift_early",
    "excess lunch": "excess_lunch",
    "ot": "ot", "o.t.": "ot", "overtime": "ot",
    "ot amount": "ot_amount",
    "os": "os",
    "manual": "manual",
    "in temperature": "in_temp", "in temperature desc": "in_temp_desc",
    "out temperature": "out_temp", "out temperature desc": "out_temp_desc",
}

TIME_FIELDS = ("shift_start", "time_in", "lunch_out", "lunch_in", "time_out")
DURATION_FIELDS = ("hrs", "early", "late", "shift_early", "excess_lunch", "ot", "os")

STATUS_LABELS: Dict[str, str] = {
    "P": "Present",
    "A": "Absent",
    "HLF": "Half day",
    "MIS": "Mispunch",
    "WO": "Weekly off",
    "HO": "Holiday",
    "HLD": "Holiday",
    "L": "Leave",
    "CL": "Casual leave",
    "SL": "Sick leave",
    "EL": "Earned leave",
    "PL": "Paid leave",
    "LWP": "Leave without pay",
    "OD": "On duty",
    "T": "Tour",
    "WOP": "Weekly off (present)",
    "HOP": "Holiday (present)",
}

DEPT_RE = re.compile(r"department\s+code\s*&?\s*name\s*:\s*(\S+)\s*(.*)$", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")
REPORT_DATE_RE = re.compile(r"for\s+date\s*:?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})", re.IGNORECASE)
RUN_RE = re.compile(r"run\s+date\s*&?\s*time\s*:?\s*(.+)$", re.IGNORECASE)
COMPANY_RE = re.compile(r"company\s+name\s*:?\s*(.+)$", re.IGNORECASE)
FILENAME_DATE_RE = re.compile(
    r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})",
    re.IGNORECASE,
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class ParseError(ValueError):
    """Raised when the uploaded file is not a recognisable Starlink report."""


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------

@dataclass
class ParsedRecord:
    seq: int
    dept_code: Optional[str]
    dept_name: Optional[str]
    paycode: str
    name: str
    segment: str = "other"
    card_no: Optional[str] = None
    shift: Optional[str] = None
    shift_start: Optional[str] = None
    time_in: Optional[str] = None
    lunch_out: Optional[str] = None
    lunch_in: Optional[str] = None
    time_out: Optional[str] = None
    hrs: Optional[str] = None
    hrs_minutes: Optional[int] = None
    status: str = ""
    status_label: str = ""
    early: Optional[str] = None
    late: Optional[str] = None
    shift_early: Optional[str] = None
    excess_lunch: Optional[str] = None
    ot: Optional[str] = None
    ot_amount: Optional[str] = None
    os: Optional[str] = None
    manual: bool = False
    is_present: bool = False
    is_absent: bool = False
    is_halfday: bool = False
    is_mispunch: bool = False
    is_night: bool = False
    has_warning: bool = False
    notes: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedReport:
    report_date: Optional[date]
    company: Optional[str]
    title: Optional[str]
    run_at: Optional[str]
    source_filename: Optional[str]
    columns: List[str]
    records: List[ParsedRecord]
    departments: List[Tuple[str, str]]
    problems: List[str] = field(default_factory=list)

    # -- summary helpers ----------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.records)

    def count(self, attr: str) -> int:
        return sum(1 for r in self.records if getattr(r, attr))

    def summary(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for r in self.records:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        depts = []
        for code, name in self.departments:
            rs = [r for r in self.records if r.dept_code == code and r.dept_name == name]
            depts.append({
                "code": code, "name": name, "total": len(rs),
                "present": sum(r.is_present for r in rs),
                "absent": sum(r.is_absent for r in rs),
                "halfday": sum(r.is_halfday for r in rs),
                "mispunch": sum(r.is_mispunch for r in rs),
                "warnings": sum(r.has_warning for r in rs),
            })
        segs: Dict[str, int] = {}
        for r in self.records:
            segs[r.segment or "other"] = segs.get(r.segment or "other", 0) + 1
        return {
            "total": self.total,
            "by_segment": segs,
            "present": self.count("is_present"),
            "absent": self.count("is_absent"),
            "halfday": self.count("is_halfday"),
            "mispunch": self.count("is_mispunch"),
            "night": self.count("is_night"),
            "manual": self.count("manual"),
            "warnings": self.count("has_warning"),
            "by_status": by_status,
            "departments": depts,
        }


# ----------------------------------------------------------------------------
# Grid loading (xlsx / xls / csv -> list of rows of raw cell values)
# ----------------------------------------------------------------------------

Grid = List[List[Any]]


def load_grid(data: bytes, filename: Optional[str] = None) -> Grid:
    name = (filename or "").lower()
    head = data[:8]
    if head.startswith(b"PK\x03\x04") or name.endswith((".xlsx", ".xlsm")):
        return _grid_from_xlsx(data)
    if head.startswith(b"\xd0\xcf\x11\xe0") or name.endswith(".xls"):
        return _grid_from_xls(data)
    if name.endswith((".csv", ".txt")) or _looks_like_text(data):
        return _grid_from_csv(data)
    raise ParseError("Unsupported file type. Please upload the Starlink .xlsx, .xls or .csv export.")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in sample


def _grid_from_xlsx(data: bytes) -> Grid:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # pragma: no cover - depends on corrupt input
        raise ParseError(f"Could not open workbook: {exc}") from exc
    grid: Grid = []
    best: Grid = []
    for ws in wb.worksheets:
        grid = [list(row) for row in ws.iter_rows(values_only=True)]
        if any(_norm(c) == "paycode" for row in grid for c in row if isinstance(c, str)):
            wb.close()
            return grid
        if len(grid) > len(best):
            best = grid
    wb.close()
    return best


def _grid_from_xls(data: bytes) -> Grid:
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=data, formatting_info=False)
    except Exception as exc:  # pragma: no cover
        raise ParseError(f"Could not open .xls workbook: {exc}") from exc
    best: Grid = []
    for sh in book.sheets():
        grid: Grid = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                v: Any = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        v = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                    except Exception:  # pragma: no cover
                        pass
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    v = None
                row.append(v)
            grid.append(row)
        if any(_norm(c) == "paycode" for row in grid for c in row if isinstance(c, str)):
            return grid
        if len(grid) > len(best):
            best = grid
    return best


def _grid_from_csv(data: bytes) -> Grid:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


# ----------------------------------------------------------------------------
# Cell helpers
# ----------------------------------------------------------------------------

def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower() if v is not None else ""


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def parse_time(v: Any) -> Optional[str]:
    """Return 'HH:MM' or None for the many shapes a time cell can take."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, timedelta):
        total = int(v.total_seconds()) // 60
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"
    if isinstance(v, (int, float)):
        # Excel fraction of a day
        if 0 <= float(v) < 1:
            total = int(round(float(v) * 24 * 60))
            return f"{(total // 60) % 24:02d}:{total % 60:02d}"
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?(?:\s*([AaPp])[Mm]?)?$", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    ap = m.group(3)
    if ap:
        ap = ap.lower()
        if ap == "p" and h < 12:
            h += 12
        if ap == "a" and h == 12:
            h = 0
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_duration(v: Any) -> Tuple[Optional[str], Optional[int]]:
    """Starlink prints durations as H.MM (12.14 == 12 h 14 min). Returns (text, minutes)."""
    if v is None:
        return None, None
    if isinstance(v, (time, datetime)):
        t = v if isinstance(v, time) else v.time()
        return f"{t.hour}.{t.minute:02d}", t.hour * 60 + t.minute
    s = str(v).strip()
    if not s:
        return None, None
    if isinstance(v, float):
        s = f"{v:.2f}"
    m = re.match(r"^(\d*)[.:](\d{1,2})$", s)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2).ljust(2, "0"))
        return f"{h}.{mi:02d}", h * 60 + mi
    if s.isdigit():
        return f"{int(s)}.00", int(s) * 60
    return s, None


def _minutes(hhmm: Optional[str]) -> Optional[int]:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fmt_minutes(mins: Optional[int]) -> str:
    if mins is None:
        return ""
    return f"{mins // 60}.{mins % 60:02d}"


def _parse_date(s: str) -> Optional[date]:
    m = DATE_RE.search(s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        try:  # maybe mm/dd/yyyy
            return date(y, d, mo)
        except ValueError:
            return None


def date_from_filename(filename: Optional[str]) -> Optional[date]:
    if not filename:
        return None
    m = FILENAME_DATE_RE.search(filename)
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        except (ValueError, KeyError):
            return None
    m = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", filename)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return _parse_date(filename)


# ----------------------------------------------------------------------------
# Main parse
# ----------------------------------------------------------------------------

def parse_report(data: bytes, filename: Optional[str] = None,
                 report_date_override: Optional[date] = None) -> ParsedReport:
    grid = load_grid(data, filename)
    return parse_grid(grid, filename, report_date_override)


def parse_grid(grid: Grid, filename: Optional[str] = None,
               report_date_override: Optional[date] = None) -> ParsedReport:
    header_idx, colmap = _find_header(grid)
    if header_idx is None:
        raise ParseError(
            "Could not find the report header row (expected columns such as 'PayCode', "
            "'Employee Name', 'In', 'Out', 'Status'). Is this a Starlink daily report?"
        )
    missing = [k for k in ("paycode", "name", "status") if k not in colmap]
    if missing:
        raise ParseError(f"Header row found but required column(s) missing: {', '.join(missing)}")

    company = title = run_at = None
    report_date: Optional[date] = None
    for row in grid[:header_idx]:
        for cell in row:
            if not isinstance(cell, str):
                continue
            s = cell.strip()
            if not s:
                continue
            if (m := COMPANY_RE.search(s)) and company is None:
                company = m.group(1).strip()
            elif (m := RUN_RE.search(s)) and run_at is None:
                run_at = m.group(1).strip()
            elif (m := REPORT_DATE_RE.search(s)):
                report_date = _parse_date(m.group(1))
                title = re.split(r"\s+for\s+date", s, flags=re.IGNORECASE)[0].strip() or None
            elif "report" in s.lower() and title is None:
                title = s

    problems: List[str] = []
    if report_date_override:
        report_date = report_date_override
    if report_date is None:
        report_date = date_from_filename(filename)
        if report_date:
            problems.append("Report date was not printed in the sheet; taken from the file name.")
    if report_date is None:
        problems.append("Report date could not be determined. Please set it manually.")

    records: List[ParsedRecord] = []
    departments: List[Tuple[str, str]] = []
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    seq = 0
    long_span_min = int(settings.long_span_hours * 60)

    for row in grid[header_idx + 1:]:
        if row is None:
            continue
        cells = list(row)
        # Department banner rows: "** Department Code & Name : 001  A.O.D"
        banner = next((c for c in cells if isinstance(c, str) and DEPT_RE.search(c)), None)
        if banner is not None:
            m = DEPT_RE.search(banner)
            dept_code = m.group(1).strip()
            dept_name = re.sub(r"\s+", " ", m.group(2)).strip() or dept_code
            if (dept_code, dept_name) not in departments:
                departments.append((dept_code, dept_name))
            continue

        paycode = _text(_get(cells, colmap, "paycode"))
        name = _text(_get(cells, colmap, "name"))
        if not paycode or not name:
            continue
        if _norm(paycode) == "paycode":  # repeated header on a new page
            continue
        if re.match(r"^(total|grand total|department total)", _norm(paycode)):
            continue

        seq += 1
        rec = ParsedRecord(seq=seq, dept_code=dept_code, dept_name=dept_name,
                           paycode=paycode, name=re.sub(r"\s+", " ", name))
        rec.segment = segment_for(paycode)
        rec.card_no = _text(_get(cells, colmap, "card_no"))
        rec.shift = _text(_get(cells, colmap, "shift"))
        for f in TIME_FIELDS:
            setattr(rec, f, parse_time(_get(cells, colmap, f)))
        for f in DURATION_FIELDS:
            txt, mins = parse_duration(_get(cells, colmap, f))
            setattr(rec, f, txt)
            if f == "hrs":
                rec.hrs_minutes = mins
        rec.ot_amount = _text(_get(cells, colmap, "ot_amount"))
        manual = _text(_get(cells, colmap, "manual"))
        rec.manual = bool(manual) and manual.upper() in ("Y", "YES", "1", "TRUE", "M")
        rec.status = (_text(_get(cells, colmap, "status")) or "").upper()
        rec.status_label = STATUS_LABELS.get(rec.status, rec.status or "Unknown")
        for f in ("in_temp", "in_temp_desc", "out_temp", "out_temp_desc"):
            v = _text(_get(cells, colmap, f))
            if v:
                rec.extra[f] = v
        rec.raw = " | ".join("" if c is None else str(c).strip() for c in cells).strip(" |")

        classify(rec, long_span_min)
        records.append(rec)

    if not records:
        raise ParseError("The header row was found but no employee rows followed it.")

    return ParsedReport(
        report_date=report_date, company=company, title=title, run_at=run_at,
        source_filename=filename, columns=list(colmap.keys()), records=records,
        departments=departments, problems=problems,
    )


def segment_for(paycode: Optional[str]) -> str:
    """PayCodes starting with 1 are Indian staff, 2 are local staff (configurable)."""
    pc = (paycode or "").strip()
    for prefix, seg in sorted(settings.segment_map().items(), key=lambda kv: -len(kv[0])):
        if pc.startswith(prefix):
            return seg
    return "other"


def _get(cells: Sequence[Any], colmap: Dict[str, int], key: str) -> Any:
    idx = colmap.get(key)
    if idx is None or idx >= len(cells):
        return None
    return cells[idx]


def _find_header(grid: Grid) -> Tuple[Optional[int], Dict[str, int]]:
    for i, row in enumerate(grid[:60]):
        if row is None:
            continue
        found: Dict[str, int] = {}
        for j, cell in enumerate(row):
            key = HEADER_ALIASES.get(_norm(cell))
            if key and key not in found:
                found[key] = j
        if "paycode" in found and "name" in found:
            return i, found
    return None, {}


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------

def classify(rec: ParsedRecord, long_span_min: Optional[int] = None) -> None:
    """Derive the attention flags for a record. Mutates ``rec`` in place."""
    if long_span_min is None:
        long_span_min = int(settings.long_span_hours * 60)
    st = rec.status
    notes: List[str] = []

    rec.is_present = st in ("P", "WOP", "HOP", "OD", "T")
    rec.is_absent = st == "A"
    rec.is_halfday = st == "HLF"
    rec.is_mispunch = st == "MIS"

    has_in, has_out = bool(rec.time_in), bool(rec.time_out)

    if rec.is_halfday:
        detail = f" ({rec.hrs} h worked)" if rec.hrs else ""
        notes.append("Half day as per machine" + detail)
    if st == "MIS":
        if has_in and not has_out:
            notes.append(f"Mispunch: IN at {rec.time_in}, no OUT punch")
        elif has_out and not has_in:
            notes.append(f"Mispunch: OUT at {rec.time_out}, no IN punch")
        else:
            notes.append("Mispunch as per machine")
    elif has_in != has_out and not rec.is_absent:
        # Machine said P/HLF/etc. but only one punch exists – treat as a mispunch too.
        rec.is_mispunch = True
        which = "OUT" if has_in else "IN"
        notes.append(f"Only one punch recorded ({which} missing) – treated as mispunch")

    if has_in and has_out:
        i, o = _minutes(rec.time_in), _minutes(rec.time_out)
        if o is not None and i is not None and o < i:
            rec.is_night = True
    if rec.shift and rec.shift.upper().startswith("N"):
        rec.is_night = True

    if rec.hrs_minutes is not None and rec.hrs_minutes >= long_span_min and not rec.is_absent:
        rec.has_warning = True
        notes.append(f"Worked span {rec.hrs} h – probably a missed punch paired with the next day")

    if has_in and has_out and rec.time_in == rec.time_out and rec.hrs_minutes in (0, None):
        rec.has_warning = True
        notes.append("IN and OUT are identical – single punch registered twice")

    if rec.late:
        notes.append(f"Late by {rec.late}")
    if rec.shift_early:
        notes.append(f"Left early by {rec.shift_early}")
    if rec.ot:
        notes.append(f"OT {rec.ot}")
    if rec.manual:
        notes.append("Manual punch")

    rec.notes = notes
