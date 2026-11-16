"""Turn SQL result rows into ParsedReports – the same shape the Excel parser produces."""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import settings
from ..parser import (
    STATUS_LABELS, ParsedRecord, ParsedReport, classify, fmt_minutes, parse_duration, parse_time, segment_for,
)

# ----------------------------------------------------------------------------
# Field catalogue (what a pull job can map onto)
# ----------------------------------------------------------------------------

FIELDS: List[Dict[str, Any]] = [
    {"key": "paycode", "label": "PayCode / employee code", "required": True, "group": "identity",
     "hint": "Starts with 1 for Indian staff, 2 for local staff"},
    {"key": "name", "label": "Employee name", "required": True, "group": "identity"},
    {"key": "card_no", "label": "Card number", "required": False, "group": "identity"},
    {"key": "dept_code", "label": "Department code", "required": False, "group": "identity"},
    {"key": "dept_name", "label": "Department name", "required": False, "group": "identity"},
    {"key": "report_date", "label": "Attendance date", "required": False, "group": "day",
     "hint": "Needed when one query returns several days; otherwise the run date is used"},
    {"key": "shift", "label": "Shift code", "required": False, "group": "day"},
    {"key": "shift_start", "label": "Shift start", "required": False, "group": "day"},
    {"key": "time_in", "label": "In punch", "required": False, "group": "punches"},
    {"key": "lunch_out", "label": "Lunch out", "required": False, "group": "punches"},
    {"key": "lunch_in", "label": "Lunch in", "required": False, "group": "punches"},
    {"key": "time_out", "label": "Out punch", "required": False, "group": "punches"},
    {"key": "hrs", "label": "Hours worked", "required": False, "group": "punches",
     "hint": "Computed from In/Out when not mapped"},
    {"key": "status", "label": "Status (P / A / MIS / HLF)", "required": False, "group": "punches",
     "hint": "Derived from the punches when not mapped"},
    {"key": "early", "label": "Early arrival", "required": False, "group": "extras"},
    {"key": "late", "label": "Late arrival", "required": False, "group": "extras"},
    {"key": "shift_early", "label": "Left early", "required": False, "group": "extras"},
    {"key": "ot", "label": "Overtime", "required": False, "group": "extras"},
    {"key": "manual", "label": "Manual punch flag", "required": False, "group": "extras"},
]
PUNCH_FIELDS: List[Dict[str, Any]] = [
    {"key": "paycode", "label": "PayCode / employee code", "required": True, "group": "punch"},
    {"key": "punch_time", "label": "Punch date-time", "required": True, "group": "punch",
     "hint": "A datetime column, or map date and time separately"},
    {"key": "punch_date", "label": "Punch date (if separate)", "required": False, "group": "punch"},
    {"key": "direction", "label": "Direction (in/out)", "required": False, "group": "punch",
     "hint": "IN/OUT, 0/1, I/O … used to prefer IN for the first and OUT for the last punch"},
]

ALIASES: Dict[str, str] = {
    "paycode": "paycode", "pay_code": "paycode", "empcode": "paycode", "emp_code": "paycode", "employeecode": "paycode",
    "employee_code": "paycode", "empid": "paycode", "emp_id": "paycode", "employeeid": "paycode", "code": "paycode",
    "staffcode": "paycode", "userid": "paycode", "user_id": "paycode", "enrollno": "paycode",
    "name": "name", "empname": "name", "emp_name": "name", "employeename": "name", "employee_name": "name", "staffname": "name",
    "cardno": "card_no", "card_no": "card_no", "presentcardno": "card_no", "card": "card_no", "cardnumber": "card_no",
    "departmentcode": "dept_code", "deptcode": "dept_code", "dept_code": "dept_code", "department_code": "dept_code",
    "departmentname": "dept_name", "deptname": "dept_name", "dept_name": "dept_name", "department": "dept_name",
    "department_name": "dept_name", "dept": "dept_name",
    "dateoffice": "report_date", "date": "report_date", "attdate": "report_date", "att_date": "report_date",
    "attendancedate": "report_date", "attendance_date": "report_date", "reportdate": "report_date", "report_date": "report_date",
    "workdate": "report_date", "logdate": "report_date", "day": "report_date", "fordate": "report_date",
    "shift": "shift", "shiftcode": "shift", "shift_code": "shift", "shiftattended": "shift", "shift_attended": "shift",
    "shiftstart": "shift_start", "shift_start": "shift_start", "shiftstarttime": "shift_start", "starttime": "shift_start",
    "in": "time_in", "in1": "time_in", "intime": "time_in", "in_time": "time_in", "timein": "time_in", "time_in": "time_in",
    "punchin": "time_in", "punch_in": "time_in", "firstin": "time_in", "first_in": "time_in", "firstpunch": "time_in",
    "out": "time_out", "out2": "time_out", "outtime": "time_out", "out_time": "time_out", "timeout": "time_out",
    "time_out": "time_out", "punchout": "time_out", "punch_out": "time_out", "lastout": "time_out", "last_out": "time_out",
    "lastpunch": "time_out",
    "lunchout": "lunch_out", "lunch_out": "lunch_out", "lunchin": "lunch_in", "lunch_in": "lunch_in",
    "hrsworks": "hrs", "hoursworked": "hrs", "hours_worked": "hrs", "hrs": "hrs", "workhrs": "hrs", "workhours": "hrs",
    "work_hours": "hrs", "hours": "hrs", "duration": "hrs", "totalhours": "hrs", "total_hours": "hrs",
    "status": "status", "attstatus": "status", "attendancestatus": "status", "attendance_status": "status", "presentabsent": "status",
    "early": "early", "earlyarrival": "early", "early_arrival": "early", "earlyarriv": "early",
    "late": "late", "latearrival": "late", "late_arrival": "late", "latearriv": "late", "lateby": "late",
    "earlydeparture": "shift_early", "early_departure": "shift_early", "shiftearly": "shift_early", "earlyout": "shift_early",
    "ot": "ot", "overtime": "ot", "over_time": "ot", "otduration": "ot", "othours": "ot",
    "manual": "manual", "ismanual": "manual", "manualpunch": "manual",
    # punch log
    "punchtime": "punch_time", "punch_time": "punch_time", "logtime": "punch_time", "log_time": "punch_time",
    "punchdatetime": "punch_time", "datetime": "punch_time", "officepunch": "punch_time", "eventtime": "punch_time",
    "timestamp": "punch_time", "punchdate": "punch_date", "punch_date": "punch_date",
    "direction": "direction", "inout": "direction", "in_out": "direction", "iotype": "direction", "io": "direction",
    "punchtype": "direction", "punch_type": "direction", "eventtype": "direction", "type": "direction",
}
DEFAULT_STATUS_MAP = {
    "P": "P", "PRESENT": "P", "PR": "P", "1": "P",
    "A": "A", "ABSENT": "A", "AB": "A", "0": "A",
    "MIS": "MIS", "MISPUNCH": "MIS", "MISSPUNCH": "MIS", "MP": "MIS", "SINGLE PUNCH": "MIS",
    "HLF": "HLF", "HALF": "HLF", "HALFDAY": "HLF", "HALF DAY": "HLF", "HD": "HLF", "H": "HLF",
    "WO": "WO", "WEEKLY OFF": "WO", "WEEKOFF": "WO", "OFF": "WO",
    "HO": "HO", "HOLIDAY": "HO", "HLD": "HO",
    "L": "L", "LEAVE": "L", "CL": "CL", "SL": "SL", "EL": "EL", "PL": "PL", "LWP": "LWP", "OD": "OD", "T": "T",
}


def norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def suggest_mapping(columns: Iterable[str], punch_mode: bool = False) -> Dict[str, str]:
    cols = list(columns)
    found: Dict[str, str] = {}
    by_norm = {norm_col(c): c for c in cols}
    # Starlink tblTimeRegister convention: In1/Out1 = first in / lunch out, In2/Out2 = lunch in / last out
    if "out2" in by_norm and "out1" in by_norm:
        found["time_out"] = by_norm["out2"]
        found["lunch_out"] = by_norm["out1"]
        if "in2" in by_norm:
            found["lunch_in"] = by_norm["in2"]
    for c in cols:
        n = norm_col(c)
        key = ALIASES.get(n) or ALIASES.get(n.replace("_", ""))
        if key is None:
            continue
        if punch_mode and key in ("time_in", "time_out") and n in ("in", "out"):
            continue
        if key == "time_out" and n == "out1" and "time_out" in found:
            continue
        found.setdefault(key, c)
    return found


# ----------------------------------------------------------------------------
# Value helpers
# ----------------------------------------------------------------------------

def to_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date() if fmt.endswith("%S") else datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def to_datetime(v: Any, date_hint: Any = None) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, time())
    if isinstance(v, time):
        d = to_date(date_hint)
        return datetime.combine(d, v) if d else None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%d %b %Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    hhmm = parse_time(s)
    d = to_date(date_hint)
    if hhmm and d:
        h, m = hhmm.split(":")
        return datetime.combine(d, time(int(h), int(m)))
    return None


def time_of(v: Any) -> Optional[str]:
    """HH:MM from a time, datetime, 'HH:MM[:SS]' or a full datetime string (SQLite/ODBC style)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    if isinstance(v, time):
        return v.strftime("%H:%M")
    hhmm = parse_time(v)
    if hhmm:
        return hhmm
    dt = to_datetime(v)
    return dt.strftime("%H:%M") if dt else None


def hours_value(v: Any, hrs_format: str = "auto") -> Tuple[Optional[str], Optional[int]]:
    if v is None or v == "":
        return None, None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        fmt = hrs_format
        if fmt == "auto":
            fmt = "decimal"
        if fmt == "minutes":
            mins = int(round(float(v)))
        elif fmt == "hmm":
            h = int(float(v))
            mins = h * 60 + int(round((float(v) - h) * 100))
        else:  # decimal hours
            mins = int(round(float(v) * 60))
        return fmt_minutes(mins), mins
    if isinstance(v, timedelta):
        mins = int(v.total_seconds() // 60)
        return fmt_minutes(mins), mins
    return parse_duration(v)


def norm_status(v: Any, status_map: Optional[Dict[str, str]] = None) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    m = {**DEFAULT_STATUS_MAP, **{k.upper(): val for k, val in (status_map or {}).items()}}
    return m.get(s, s[:10])


def truthy(v: Any) -> bool:
    return str(v).strip().upper() in ("Y", "YES", "1", "TRUE", "M", "T")


def _val(row: Dict[str, Any], mapping: Dict[str, str], key: str) -> Any:
    col = mapping.get(key)
    if not col:
        return None
    return row.get(col)


# ----------------------------------------------------------------------------
# Daily rows -> reports
# ----------------------------------------------------------------------------

def build_record(seq: int, row: Dict[str, Any], mapping: Dict[str, str], options: Dict[str, Any],
                 row_date: Optional[date]) -> Optional[ParsedRecord]:
    paycode = _val(row, mapping, "paycode")
    name = _val(row, mapping, "name")
    if paycode is None or str(paycode).strip() == "":
        return None
    if isinstance(paycode, float) and paycode.is_integer():
        paycode = int(paycode)
    paycode = str(paycode).strip()
    name = re.sub(r"\s+", " ", str(name or "").strip()) or paycode
    dept_code = _val(row, mapping, "dept_code")
    dept_name = _val(row, mapping, "dept_name")
    if isinstance(dept_code, float) and dept_code.is_integer():
        dept_code = int(dept_code)
    dept_code = str(dept_code).strip() if dept_code not in (None, "") else None
    dept_name = re.sub(r"\s+", " ", str(dept_name).strip()) if dept_name not in (None, "") else None
    if dept_code is None and dept_name:
        dept_code = dept_name
    if dept_name is None and dept_code:
        dept_name = dept_code
    rec = ParsedRecord(seq=seq, dept_code=dept_code or "000", dept_name=dept_name or "UNASSIGNED",
                       paycode=paycode, name=name)
    rec.segment = segment_for(paycode)
    cn = _val(row, mapping, "card_no")
    rec.card_no = str(cn).strip() if cn not in (None, "") else None
    sh = _val(row, mapping, "shift")
    rec.shift = str(sh).strip() if sh not in (None, "") else None
    rec.shift_start = time_of(_val(row, mapping, "shift_start"))
    for f in ("time_in", "lunch_out", "lunch_in", "time_out"):
        setattr(rec, f, time_of(_val(row, mapping, f)))
    hrs_fmt = options.get("hrs_format", "auto")
    rec.hrs, rec.hrs_minutes = hours_value(_val(row, mapping, "hrs"), hrs_fmt)
    if rec.hrs_minutes is None and rec.time_in and rec.time_out:
        i = int(rec.time_in[:2]) * 60 + int(rec.time_in[3:])
        o = int(rec.time_out[:2]) * 60 + int(rec.time_out[3:])
        rec.hrs_minutes = (o - i) % (24 * 60)
        rec.hrs = fmt_minutes(rec.hrs_minutes)
    for f in ("early", "late", "shift_early", "ot"):
        txt, _ = hours_value(_val(row, mapping, f), hrs_fmt)
        setattr(rec, f, txt if txt not in ("0.00",) else None)
    rec.manual = truthy(_val(row, mapping, "manual")) if mapping.get("manual") else False
    st = norm_status(_val(row, mapping, "status"), options.get("status_map"))
    if not st:
        st = "P" if (rec.time_in and rec.time_out) else ("MIS" if (rec.time_in or rec.time_out) else "A")
    rec.status = st
    rec.status_label = STATUS_LABELS.get(st, st)
    rec.raw = " | ".join(f"{k}={'' if v is None else v}" for k, v in row.items())[:2000]
    classify(rec)
    return rec


def rows_to_reports(rows: List[Dict[str, Any]], mapping: Dict[str, str], options: Optional[Dict[str, Any]],
                    default_date: Optional[date], source_label: str) -> List[ParsedReport]:
    options = options or {}
    by_day: Dict[Optional[date], List[Dict[str, Any]]] = defaultdict(list)
    date_col = mapping.get("report_date")
    for r in rows:
        d = to_date(r.get(date_col)) if date_col else None
        by_day[d or default_date].append(r)
    reports: List[ParsedReport] = []
    for d, day_rows in sorted(by_day.items(), key=lambda kv: (kv[0] is None, kv[0] or date.min)):
        recs: List[ParsedRecord] = []
        for r in day_rows:
            rec = build_record(len(recs) + 1, r, mapping, options, d)
            if rec:
                recs.append(rec)
        if not recs:
            continue
        recs.sort(key=lambda x: (x.dept_code or "", x.paycode))
        for i, rec in enumerate(recs, start=1):
            rec.seq = i
        depts: List[Tuple[str, str]] = []
        for rec in recs:
            if (rec.dept_code, rec.dept_name) not in depts:
                depts.append((rec.dept_code, rec.dept_name))
        problems = [] if d else ["Attendance date could not be determined for these rows."]
        reports.append(ParsedReport(report_date=d, company=options.get("company"), title="DAILY PERFORMANCE REPORT",
                                    run_at=datetime.now().strftime("%d/%m/%Y %H:%M"), source_filename=source_label,
                                    columns=list(mapping.keys()), records=recs, departments=depts, problems=problems))
    return reports


# ----------------------------------------------------------------------------
# Punch log -> reports
# ----------------------------------------------------------------------------

def _is_out(v: Any) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("OUT", "O", "1", "EXIT", "E", "CHECK-OUT", "CHECKOUT", "OFF"):
        return True
    if s in ("IN", "I", "0", "ENTRY", "CHECK-IN", "CHECKIN", "ON"):
        return False
    return None


def punches_to_reports(employees: List[Dict[str, Any]], emp_mapping: Dict[str, str],
                       punches: List[Dict[str, Any]], punch_mapping: Dict[str, str],
                       options: Optional[Dict[str, Any]], dates: List[date], source_label: str) -> List[ParsedReport]:
    options = options or {}
    day_start = parse_time(options.get("day_start") or "00:00") or "00:00"
    ds_h, ds_m = int(day_start[:2]), int(day_start[3:])
    max_span = timedelta(hours=float(options.get("max_span_hours") or 16))
    pcol, tcol, dcol, dircol = (punch_mapping.get("paycode"), punch_mapping.get("punch_time"),
                                punch_mapping.get("punch_date"), punch_mapping.get("direction"))
    per_emp: Dict[str, List[Tuple[datetime, Optional[bool]]]] = defaultdict(list)
    for p in punches:
        pc = p.get(pcol)
        if pc in (None, ""):
            continue
        if isinstance(pc, float) and pc.is_integer():
            pc = int(pc)
        ts = to_datetime(p.get(tcol), p.get(dcol) if dcol else None)
        if ts is None:
            continue
        per_emp[str(pc).strip()].append((ts, _is_out(p.get(dircol)) if dircol else None))
    for lst in per_emp.values():
        lst.sort(key=lambda x: x[0])
    used: Dict[str, set] = defaultdict(set)

    reports: List[ParsedReport] = []
    for d in dates:
        window_start = datetime.combine(d, time(ds_h, ds_m))
        window_end = window_start + timedelta(days=1)
        recs: List[ParsedRecord] = []
        for emp in employees:
            row = dict(emp)
            pc = _val(row, emp_mapping, "paycode")
            if pc in (None, ""):
                continue
            if isinstance(pc, float) and pc.is_integer():
                pc = int(pc)
            pc = str(pc).strip()
            events = [(i, ts, out) for i, (ts, out) in enumerate(per_emp.get(pc, []))
                      if window_start <= ts < window_end + max_span and i not in used[pc]]
            # first punch inside the day window that is not explicitly an OUT
            first = next(((i, ts) for i, ts, out in events if ts < window_end and out is not True), None)
            if first is None:
                first = next(((i, ts) for i, ts, out in events if ts < window_end), None)
            t_in = t_out = None
            if first is not None:
                fi, fts = first
                used[pc].add(fi)
                t_in = fts
                candidates = [(i, ts) for i, ts, out in events if i != fi and fts < ts <= fts + max_span and out is not False]
                if not candidates:
                    candidates = [(i, ts) for i, ts, out in events if i != fi and fts < ts <= fts + max_span]
                if candidates:
                    li, lts = candidates[-1]
                    for i, ts in candidates:
                        used[pc].add(i)
                    t_out = lts
            row["__in"] = t_in.strftime("%H:%M") if t_in else None
            row["__out"] = t_out.strftime("%H:%M") if t_out else None
            m = dict(emp_mapping)
            m["time_in"], m["time_out"] = "__in", "__out"
            m.pop("status", None)
            m.pop("hrs", None)
            rec = build_record(len(recs) + 1, row, m, options, d)
            if rec:
                if t_in and t_out:
                    rec.hrs_minutes = int((t_out - t_in).total_seconds() // 60)
                    rec.hrs = fmt_minutes(rec.hrs_minutes)
                    classify(rec)
                recs.append(rec)
        if not recs:
            continue
        recs.sort(key=lambda x: (x.dept_code or "", x.paycode))
        for i, rec in enumerate(recs, start=1):
            rec.seq = i
        depts: List[Tuple[str, str]] = []
        for rec in recs:
            if (rec.dept_code, rec.dept_name) not in depts:
                depts.append((rec.dept_code, rec.dept_name))
        reports.append(ParsedReport(report_date=d, company=options.get("company"), title="DAILY PERFORMANCE REPORT",
                                    run_at=datetime.now().strftime("%d/%m/%Y %H:%M"), source_filename=source_label,
                                    columns=list(emp_mapping.keys()) + ["time_in", "time_out"], records=recs,
                                    departments=depts, problems=[]))
    return reports


def rows_to_csv_bytes(rows: List[Dict[str, Any]], columns: Optional[List[str]] = None) -> bytes:
    buf = io.StringIO()
    if not rows:
        return b""
    cols = columns or list(rows[0].keys())
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue().encode("utf-8-sig")
