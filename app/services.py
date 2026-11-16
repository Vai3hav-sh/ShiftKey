"""Persistence + business rules: scoping, reports, corrections, reference data."""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .config import SEGMENT_LABELS
from .exporter import group_by_department, report_date_label
from .models import (
    ROLE_ADMIN,
    ROLE_HOD,
    ROLE_HR,
    AttendanceRecord,
    Correction,
    Department,
    Report,
    Shift,
    User,
    utcnow,
)
from .parser import STATUS_LABELS, ParsedReport, fmt_minutes

RECORD_FIELDS = (
    "seq", "dept_code", "dept_name", "paycode", "segment", "card_no", "name", "shift", "shift_start",
    "time_in", "lunch_out", "lunch_in", "time_out", "hrs", "hrs_minutes", "status", "early",
    "late", "shift_early", "excess_lunch", "ot", "ot_amount", "os", "manual", "is_present",
    "is_absent", "is_halfday", "is_mispunch", "is_night", "has_warning", "notes", "extra", "raw",
)

# Shift end times are not part of the Starlink export. These defaults were inferred from
# the sample data (OT = hours - shift length) and can be edited by the admin.
DEFAULT_SHIFTS = [
    ("M07", "Morning 07:00", "07:00", "19:00"),
    ("M08", "Morning 08:00", "08:00", "20:00"),
    ("830", "General 08:30", "08:30", "19:00"),
    ("807", "Office 08:00", "08:00", "19:00"),
    ("G", "General", "08:30", "17:30"),
    ("N19", "Night 19:00", "19:00", "07:00"),
    ("N20", "Night 20:00", "20:00", "08:00"),
    ("A", "A shift", "06:00", "14:00"),
    ("B", "B shift", "14:00", "22:00"),
    ("C", "C shift", "22:00", "06:00"),
]


# ----------------------------------------------------------------------------
# Scope
# ----------------------------------------------------------------------------

class Scope:
    """What a user is allowed to see. ``None`` means unrestricted."""

    def __init__(self, user: User):
        self.user = user
        self.dept_codes: Optional[List[str]] = None
        self.segment: Optional[str] = None
        if user.role == ROLE_HOD:
            self.dept_codes = user.dept_codes
        elif user.role == ROLE_HR and user.segment and user.segment != "all":
            self.segment = user.segment

    @property
    def unrestricted(self) -> bool:
        return self.dept_codes is None and self.segment is None

    def allows(self, rec: AttendanceRecord) -> bool:
        if self.dept_codes is not None and rec.dept_code not in self.dept_codes:
            return False
        if self.segment is not None and rec.segment != self.segment:
            return False
        return True

    def can_correct(self, rec: AttendanceRecord) -> bool:
        if self.user.role == ROLE_ADMIN:
            return True
        if self.user.role == ROLE_HOD:
            return rec.dept_code in (self.dept_codes or [])
        return False

    def apply(self, stmt):
        if self.dept_codes is not None:
            stmt = stmt.where(AttendanceRecord.dept_code.in_(self.dept_codes or ["__none__"]))
        if self.segment is not None:
            stmt = stmt.where(AttendanceRecord.segment == self.segment)
        return stmt

    def describe(self) -> Dict[str, Any]:
        return {
            "role": self.user.role,
            "departments": self.dept_codes,
            "segment": self.segment,
            "segment_label": SEGMENT_LABELS.get(self.segment or "all", "All"),
            "can_upload": self.user.role == ROLE_ADMIN,
            "can_manage_users": self.user.role == ROLE_ADMIN,
            "can_manage_shifts": self.user.role == ROLE_ADMIN,
            "can_correct": self.user.role in (ROLE_ADMIN, ROLE_HOD),
        }


# ----------------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------------

def seed_shifts(db: Session) -> None:
    existing = {s.code for s in db.scalars(select(Shift)).all()}
    for code, name, start, end in DEFAULT_SHIFTS:
        if code not in existing:
            db.add(Shift(code=code, name=name, start=start, end=end, source="seed"))
    db.commit()


def learn_reference_data(db: Session, parsed: ParsedReport) -> None:
    """Upsert departments and unknown shift codes seen in an import."""
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    depts = {d.code: d for d in db.scalars(select(Department)).all()}
    counts: Dict[str, int] = {}
    for r in parsed.records:
        if r.dept_code:
            counts[r.dept_code] = counts.get(r.dept_code, 0) + 1
            d = depts.get(r.dept_code)
            if d is None:
                d = Department(code=r.dept_code, name=r.dept_name or r.dept_code)
                depts[r.dept_code] = d
                db.add(d)
            if r.dept_name:
                d.name = r.dept_name
            d.last_seen = parsed.report_date
        if r.shift and r.shift not in shifts:
            s = Shift(code=r.shift, name=None, start=r.shift_start, end=None, source="import")
            shifts[r.shift] = s
            db.add(s)
        elif r.shift and shifts[r.shift].start is None and r.shift_start:
            shifts[r.shift].start = r.shift_start
    for code, n in counts.items():
        depts[code].headcount = n
    db.commit()


def backfill(db: Session) -> None:
    """Fill data added after the first release for rows imported before it:
    segments on records, and the departments / shifts reference tables."""
    from .parser import segment_for

    rows = db.scalars(select(AttendanceRecord).where(AttendanceRecord.segment.is_(None))).all()
    for r in rows:
        r.segment = segment_for(r.paycode)
    for rep in db.scalars(select(Report).where(Report.source_sha256.is_(None))).all():
        if rep.source_bytes:
            rep.source_sha256 = hashlib.sha256(rep.source_bytes).hexdigest()
            rows.append(rep)
    depts = {d.code: d for d in db.scalars(select(Department)).all()}
    shifts = {s.code for s in db.scalars(select(Shift)).all()}
    R = AttendanceRecord
    seen = db.execute(
        select(R.dept_code, R.dept_name, R.shift, R.shift_start, func.max(Report.report_date))
        .join(Report, R.report_id == Report.id)
        .group_by(R.dept_code, R.dept_name, R.shift, R.shift_start)
    ).all()
    for code, name, shift, shift_start, last in seen:
        if code and code not in depts:
            depts[code] = Department(code=code, name=name or code, last_seen=last)
            db.add(depts[code])
        if shift and shift not in shifts:
            shifts.add(shift)
            db.add(Shift(code=shift, start=shift_start, end=None, source="import"))
    if rows or seen:
        db.commit()


def list_departments(db: Session) -> List[Dict[str, Any]]:
    rows = db.scalars(select(Department).order_by(Department.code)).all()
    return [{"code": d.code, "name": d.name, "headcount": d.headcount,
             "last_seen": d.last_seen.isoformat() if d.last_seen else None} for d in rows]


def list_shifts(db: Session) -> List[Dict[str, Any]]:
    rows = db.scalars(select(Shift).order_by(Shift.code)).all()
    return [shift_dict(s) for s in rows]


def shift_dict(s: Shift) -> Dict[str, Any]:
    night = bool(s.start and s.end and s.end < s.start)
    return {"code": s.code, "name": s.name, "start": s.start, "end": s.end,
            "is_night": night, "source": s.source,
            "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None}


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

def report_snapshot(r: Report) -> Dict[str, Any]:
    """What is lost when a report is deleted or replaced – kept in the audit trail."""
    corrected = [x for x in r.records if x.is_corrected]
    return {
        "report_date": r.report_date.isoformat(), "source_filename": r.source_filename,
        "source_sha256": r.source_sha256, "uploaded_at": r.uploaded_at.isoformat() + "Z",
        "total": r.total, "present": r.present, "absent": r.absent, "halfday": r.halfday,
        "mispunch": r.mispunch, "warnings": r.warnings, "corrections": len(corrected),
        "corrected_records": [
            {"paycode": x.paycode, "name": x.name, "dept": x.dept_code, "in": x.corrected_in,
             "out": x.corrected_out, "status": x.corrected_status, "remark": x.correction_remark}
            for x in corrected[:200]
        ],
    }


def save_report(db: Session, parsed: ParsedReport, source_bytes: bytes,
                replace: bool = True) -> Tuple[Report, bool, Optional[Dict[str, Any]]]:
    if parsed.report_date is None:
        raise ValueError("Cannot store a report without a report date.")
    existing = db.scalar(select(Report).options(selectinload(Report.records)).where(Report.report_date == parsed.report_date))
    replaced = False
    previous: Optional[Dict[str, Any]] = None
    if existing is not None:
        if not replace:
            raise FileExistsError(parsed.report_date.isoformat())
        previous = report_snapshot(existing)
        db.delete(existing)
        db.flush()
        replaced = True

    s = parsed.summary()
    report = Report(
        report_date=parsed.report_date, company=parsed.company, title=parsed.title,
        run_at=parsed.run_at, source_filename=parsed.source_filename, source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest() if source_bytes else None,
        source_columns={"columns": parsed.columns, "problems": parsed.problems},
        total=s["total"], present=s["present"], absent=s["absent"],
        halfday=s["halfday"], mispunch=s["mispunch"], warnings=s["warnings"],
    )
    for r in parsed.records:
        d = r.to_dict()
        report.records.append(AttendanceRecord(**{k: d[k] for k in RECORD_FIELDS}))
    db.add(report)
    db.commit()
    db.refresh(report)
    learn_reference_data(db, parsed)
    return report, replaced, previous


def list_reports(db: Session, scope: Scope) -> List[Dict[str, Any]]:
    reports = db.scalars(select(Report).order_by(Report.report_date.desc())).all()
    if not reports:
        return []
    R = AttendanceRecord
    eff_status = func.coalesce(R.corrected_status, R.status)
    pending = case((((R.is_mispunch == True) | (R.has_warning == True)) & (R.corrected_at.is_(None)), 1), else_=0)  # noqa: E712
    stmt = select(
        R.report_id,
        func.count(R.id),
        func.sum(case((eff_status == "P", 1), else_=0)),
        func.sum(case((eff_status == "A", 1), else_=0)),
        func.sum(case((eff_status == "HLF", 1), else_=0)),
        func.sum(case((R.is_mispunch == True, 1), else_=0)),  # noqa: E712
        func.sum(case((R.has_warning == True, 1), else_=0)),  # noqa: E712
        func.sum(pending),
        func.sum(case((R.corrected_at.isnot(None), 1), else_=0)),
    ).group_by(R.report_id)
    stmt = scope.apply(stmt)
    agg = {row[0]: row[1:] for row in db.execute(stmt).all()}
    out = []
    for r in reports:
        a = agg.get(r.id)
        if a is None and not scope.unrestricted:
            continue  # nothing visible for this user that day
        a = a or (0,) * 8
        d = report_summary(r)
        d.update({"total": int(a[0]), "present": int(a[1] or 0), "absent": int(a[2] or 0),
                  "halfday": int(a[3] or 0), "mispunch": int(a[4] or 0), "warnings": int(a[5] or 0),
                  "pending": int(a[6] or 0), "corrected": int(a[7] or 0)})
        out.append(d)
    return out


def get_report(db: Session, report_id: int) -> Optional[Report]:
    return db.scalar(select(Report).options(selectinload(Report.records)).where(Report.id == report_id))


def delete_report(db: Session, report: Report) -> None:
    db.delete(report)
    db.commit()


def report_summary(r: Report) -> Dict[str, Any]:
    return {
        "id": r.id,
        "report_date": r.report_date.isoformat(),
        "report_date_label": report_date_label(r.report_date),
        "weekday": r.report_date.strftime("%A"),
        "company": r.company, "title": r.title, "run_at": r.run_at,
        "source_filename": r.source_filename, "source_kind": r.source_kind or "upload",
        "sync_job_id": r.sync_job_id, "sync_run_id": r.sync_run_id,
        "uploaded_at": r.uploaded_at.isoformat() + "Z",
        "total": r.total, "present": r.present, "absent": r.absent,
        "halfday": r.halfday, "mispunch": r.mispunch, "warnings": r.warnings,
    }


def record_dict(rec: AttendanceRecord, scope: Optional[Scope] = None,
                shifts: Optional[Dict[str, Shift]] = None) -> Dict[str, Any]:
    d = {k: getattr(rec, k) for k in RECORD_FIELDS}
    d["id"] = rec.id
    d["report_id"] = rec.report_id
    d["notes"] = d["notes"] or []
    d["extra"] = d["extra"] or {}
    d["segment_label"] = SEGMENT_LABELS.get(d["segment"] or "other", "Other")
    original = {"time_in": rec.time_in, "time_out": rec.time_out, "hrs": rec.hrs,
                "status": (rec.status or "").strip()}
    d["original"] = original
    d["flag_mispunch"] = bool(rec.is_mispunch)
    d["flag_warning"] = bool(rec.has_warning)
    d["is_corrected"] = rec.is_corrected
    if rec.is_corrected:
        d["time_in"] = rec.corrected_in
        d["time_out"] = rec.corrected_out
        d["hrs_minutes"] = rec.corrected_hrs_minutes
        d["hrs"] = fmt_minutes(rec.corrected_hrs_minutes) if rec.corrected_hrs_minutes is not None else None
        d["status"] = (rec.corrected_status or rec.status or "").strip()
        d["correction"] = {
            "at": rec.corrected_at.isoformat() + "Z", "by_id": rec.corrected_by_id,
            "remark": rec.correction_remark,
            "by": _last_correction_by(rec),
            "action": _last_correction_action(rec),
        }
    else:
        d["status"] = (rec.status or "").strip()
        d["correction"] = None
    st = d["status"]
    d["status_label"] = STATUS_LABELS.get(st, st or "Unknown")
    d["is_present"] = st in ("P", "WOP", "HOP", "OD", "T")
    d["is_absent"] = st == "A"
    d["is_halfday"] = st == "HLF"
    d["is_mispunch"] = bool(rec.is_mispunch) and not rec.is_corrected
    d["has_warning"] = bool(rec.has_warning) and not rec.is_corrected
    d["needs_action"] = (bool(rec.is_mispunch) or bool(rec.has_warning)) and not rec.is_corrected
    d["can_correct"] = scope.can_correct(rec) if scope else False
    sh = (shifts or {}).get(rec.shift or "")
    d["shift_end"] = sh.end if sh else None
    d["shift_start"] = rec.shift_start or (sh.start if sh else None)
    return d


def _last_correction_by(rec: AttendanceRecord) -> Optional[str]:
    cs = [c for c in rec.corrections if c.action != "revert"]
    return cs[-1].created_by_name if cs else None


def _last_correction_action(rec: AttendanceRecord) -> Optional[str]:
    cs = [c for c in rec.corrections if c.action != "revert"]
    return cs[-1].action if cs else None


def visible_records(report: Report, scope: Scope) -> List[AttendanceRecord]:
    return [r for r in report.records if scope.allows(r)]


def summarize(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    for x in records:
        by_status[x["status"]] = by_status.get(x["status"], 0) + 1
    depts = []
    for g in group_by_department(records):
        rs = g["records"]
        depts.append({
            "code": g["code"], "name": g["name"], "total": len(rs),
            "present": sum(1 for x in rs if x["is_present"]),
            "absent": sum(1 for x in rs if x["is_absent"]),
            "halfday": sum(1 for x in rs if x["is_halfday"]),
            "mispunch": sum(1 for x in rs if x["flag_mispunch"]),
            "pending": sum(1 for x in rs if x["needs_action"]),
            "corrected": sum(1 for x in rs if x["is_corrected"]),
            "warnings": sum(1 for x in rs if x["flag_warning"]),
        })
    segs: Dict[str, int] = {}
    for x in records:
        segs[x["segment"] or "other"] = segs.get(x["segment"] or "other", 0) + 1
    return {
        "total": len(records),
        "present": sum(1 for x in records if x["is_present"]),
        "absent": sum(1 for x in records if x["is_absent"]),
        "halfday": sum(1 for x in records if x["is_halfday"]),
        "mispunch": sum(1 for x in records if x["flag_mispunch"]),
        "warnings": sum(1 for x in records if x["flag_warning"]),
        "pending": sum(1 for x in records if x["needs_action"]),
        "corrected": sum(1 for x in records if x["is_corrected"]),
        "night": sum(1 for x in records if x["is_night"]),
        "manual": sum(1 for x in records if x["manual"]),
        "by_status": by_status,
        "by_segment": segs,
        "departments": depts,
    }


def report_detail(db: Session, r: Report, scope: Scope, replaced: bool = False) -> Dict[str, Any]:
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    records = [record_dict(x, scope, shifts) for x in visible_records(r, scope)]
    out = report_summary(r)
    out.update({
        "replaced": replaced,
        "problems": (r.source_columns or {}).get("problems", []),
        "columns": (r.source_columns or {}).get("columns", []),
        "summary": summarize(records),
        "departments": group_by_department(records),
    })
    return out


# ----------------------------------------------------------------------------
# Corrections
# ----------------------------------------------------------------------------

CORRECTION_ACTIONS = ("shift_time", "set_time", "halfday", "absent")


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def span_minutes(time_in: Optional[str], time_out: Optional[str]) -> Optional[int]:
    if not time_in or not time_out:
        return None
    i, o = _mins(time_in), _mins(time_out)
    return (o - i) % (24 * 60)


def apply_correction(db: Session, rec: AttendanceRecord, user: User, action: str,
                     field: Optional[str] = None, time_value: Optional[str] = None,
                     remark: Optional[str] = None) -> Correction:
    if action not in CORRECTION_ACTIONS:
        raise ValueError("Unknown action.")
    new_in, new_out, new_status = rec.time_in, rec.time_out, "P"

    if action in ("shift_time", "set_time"):
        if field not in ("in", "out"):
            field = "out" if (rec.time_in and not rec.time_out) else ("in" if (rec.time_out and not rec.time_in) else "out")
        if action == "shift_time":
            shift = db.get(Shift, rec.shift or "")
            if shift is None:
                raise ValueError(f"Shift '{rec.shift or '?'}' is not in the shift master; enter the time manually.")
            time_value = shift.end if field == "out" else shift.start
            if not time_value:
                raise ValueError(f"Shift {shift.code} has no {'end' if field == 'out' else 'start'} time yet. Ask the admin to set it.")
        if not time_value:
            raise ValueError("A time is required.")
        import re
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_value):
            raise ValueError("Time must be HH:MM (24 h).")
        if field == "out":
            new_out = time_value
        else:
            new_in = time_value
        if not new_in or not new_out:
            raise ValueError("Both IN and OUT must be known after the correction.")
    elif action == "halfday":
        new_status = "HLF"
    elif action == "absent":
        new_status = "A"
        new_in, new_out = None, None

    hrs = span_minutes(new_in, new_out)
    c = Correction(
        record_id=rec.id, report_id=rec.report_id, action=action, field=field,
        original_in=rec.time_in, original_out=rec.time_out, original_status=(rec.status or "").strip(),
        corrected_in=new_in, corrected_out=new_out, corrected_status=new_status,
        corrected_hrs_minutes=hrs, remark=(remark or "").strip()[:300] or None,
        created_by_id=user.id, created_by_name=user.full_name or user.username,
    )
    rec.corrected_in, rec.corrected_out, rec.corrected_status = new_in, new_out, new_status
    rec.corrected_hrs_minutes = hrs
    rec.corrected_at = utcnow()
    rec.corrected_by_id = user.id
    rec.correction_remark = c.remark
    db.add(c)
    db.commit()
    db.refresh(rec)
    return c


def revert_correction(db: Session, rec: AttendanceRecord, user: User, remark: Optional[str] = None) -> None:
    if not rec.is_corrected:
        return
    db.add(Correction(
        record_id=rec.id, report_id=rec.report_id, action="revert", field=None,
        original_in=rec.corrected_in, original_out=rec.corrected_out, original_status=rec.corrected_status,
        corrected_in=rec.time_in, corrected_out=rec.time_out, corrected_status=(rec.status or "").strip(),
        corrected_hrs_minutes=rec.hrs_minutes, remark=(remark or "").strip()[:300] or None,
        created_by_id=user.id, created_by_name=user.full_name or user.username,
    ))
    rec.corrected_in = rec.corrected_out = rec.corrected_status = None
    rec.corrected_hrs_minutes = None
    rec.corrected_at = None
    rec.corrected_by_id = None
    rec.correction_remark = None
    db.commit()
    db.refresh(rec)


def correction_dict(c: Correction, rec: AttendanceRecord, report: Report) -> Dict[str, Any]:
    return {
        "id": c.id, "record_id": c.record_id, "report_id": c.report_id,
        "report_date": report.report_date.isoformat(),
        "report_date_label": report_date_label(report.report_date),
        "paycode": rec.paycode, "name": rec.name, "dept_code": rec.dept_code, "dept_name": rec.dept_name,
        "segment": rec.segment, "shift": rec.shift,
        "action": c.action, "field": c.field,
        "original": {"time_in": c.original_in, "time_out": c.original_out, "status": c.original_status},
        "corrected": {"time_in": c.corrected_in, "time_out": c.corrected_out, "status": c.corrected_status,
                      "hrs": fmt_minutes(c.corrected_hrs_minutes) if c.corrected_hrs_minutes is not None else None},
        "remark": c.remark, "by": c.created_by_name, "at": c.created_at.isoformat() + "Z",
        "active": rec.is_corrected and c.action != "revert" and
                  (rec.corrections[-1].id == c.id if rec.corrections else False),
    }


def list_corrections(db: Session, scope: Scope, report_id: Optional[int] = None, limit: int = 500) -> List[Dict[str, Any]]:
    stmt = (select(Correction, AttendanceRecord, Report)
            .join(AttendanceRecord, Correction.record_id == AttendanceRecord.id)
            .join(Report, Correction.report_id == Report.id)
            .options(selectinload(AttendanceRecord.corrections))
            .order_by(Correction.id.desc()).limit(limit))
    if report_id:
        stmt = stmt.where(Correction.report_id == report_id)
    stmt = scope.apply(stmt)
    return [correction_dict(c, rec, rep) for c, rec, rep in db.execute(stmt).all()]


# ----------------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------------

def user_dict(u: User) -> Dict[str, Any]:
    return {
        "id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role,
        "segment": u.segment if u.role == ROLE_HR else "all",
        "segment_label": SEGMENT_LABELS.get(u.segment, u.segment) if u.role == ROLE_HR else "All",
        "departments": u.dept_codes if u.role == ROLE_HOD else [],
        "is_active": u.is_active, "must_change_password": u.must_change_password,
        "created_at": u.created_at.isoformat() + "Z",
        "last_login_at": u.last_login_at.isoformat() + "Z" if u.last_login_at else None,
    }


def me_dict(u: User) -> Dict[str, Any]:
    d = user_dict(u)
    d["scope"] = Scope(u).describe()
    return d


# ----------------------------------------------------------------------------
# Sessions (admin)
# ----------------------------------------------------------------------------

def list_sessions(db: Session) -> List[Dict[str, Any]]:
    from .models import UserSession
    rows = db.execute(select(UserSession, User).join(User, UserSession.user_id == User.id)
                      .where(UserSession.expires_at > utcnow()).order_by(UserSession.last_seen_at.desc())).all()
    return [{
        "id": s.id, "user_id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role,
        "ip": s.ip, "user_agent": s.user_agent, "created_at": s.created_at.isoformat() + "Z",
        "last_seen_at": s.last_seen_at.isoformat() + "Z", "expires_at": s.expires_at.isoformat() + "Z",
    } for s, u in rows]


def record_snapshot(rec: AttendanceRecord) -> Dict[str, Any]:
    return {
        "time_in": rec.corrected_in if rec.is_corrected else rec.time_in,
        "time_out": rec.corrected_out if rec.is_corrected else rec.time_out,
        "status": (rec.corrected_status if rec.is_corrected else rec.status or "").strip(),
        "hrs": fmt_minutes(rec.corrected_hrs_minutes if rec.is_corrected else rec.hrs_minutes)
               if (rec.corrected_hrs_minutes if rec.is_corrected else rec.hrs_minutes) is not None else None,
        "corrected": rec.is_corrected,
    }
