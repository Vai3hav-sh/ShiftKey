"""Push ShiftKey data to an external SQL table: datasets, table creation and upsert."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Integer, MetaData, String, Table, Text, and_, insert, inspect, select, update,
)
from sqlalchemy.orm import Session, selectinload

from ..models import AttendanceRecord, AuditEvent, Connection, Correction, Report, Shift, utcnow
from ..parser import STATUS_LABELS, fmt_minutes
from .engine import make_engine

# column name -> (sqlalchemy type, description)
DATASETS: Dict[str, Dict[str, Any]] = {
    "attendance": {
        "label": "Daily attendance (with corrections applied)",
        "key": ["report_date", "paycode"],
        "default_table": "shiftkey_attendance",
        "columns": [
            ("report_date", Date), ("paycode", String(40)), ("segment", String(10)), ("card_no", String(40)),
            ("name", String(160)), ("dept_code", String(20)), ("dept_name", String(120)), ("shift", String(20)),
            ("shift_start", String(5)), ("time_in", String(5)), ("time_out", String(5)), ("lunch_out", String(5)),
            ("lunch_in", String(5)), ("hrs", String(10)), ("hrs_minutes", Integer), ("status", String(10)),
            ("status_label", String(40)), ("machine_in", String(5)), ("machine_out", String(5)),
            ("machine_status", String(10)), ("machine_hrs", String(10)), ("is_corrected", Boolean),
            ("correction_action", String(20)), ("correction_remark", String(300)), ("corrected_by", String(120)),
            ("corrected_at", DateTime), ("is_halfday", Boolean), ("is_mispunch", Boolean), ("needs_review", Boolean),
            ("is_night", Boolean), ("early", String(10)), ("late", String(10)), ("shift_early", String(10)),
            ("ot", String(10)), ("manual", Boolean), ("source", String(255)), ("report_id", Integer),
            ("record_id", Integer), ("updated_at", DateTime),
        ],
    },
    "corrections": {
        "label": "Corrections log",
        "key": ["correction_id"],
        "default_table": "shiftkey_corrections",
        "columns": [
            ("correction_id", Integer), ("report_date", Date), ("paycode", String(40)), ("name", String(160)),
            ("dept_code", String(20)), ("dept_name", String(120)), ("shift", String(20)), ("action", String(20)),
            ("field", String(5)), ("original_in", String(5)), ("original_out", String(5)), ("original_status", String(10)),
            ("corrected_in", String(5)), ("corrected_out", String(5)), ("corrected_status", String(10)),
            ("corrected_hrs", String(10)), ("remark", String(300)), ("created_by", String(120)),
            ("created_at", DateTime), ("record_id", Integer), ("report_id", Integer),
        ],
    },
    "audit_events": {
        "label": "Audit trail",
        "key": ["event_id"],
        "default_table": "shiftkey_audit",
        "columns": [
            ("event_id", Integer), ("ts", DateTime), ("actor", String(60)), ("actor_role", String(10)), ("ip", String(64)),
            ("action", String(40)), ("category", String(20)), ("severity", String(10)), ("outcome", String(10)),
            ("summary", String(400)), ("target_type", String(20)), ("target_id", String(40)), ("target_label", String(200)),
            ("report_date", Date), ("dept_code", String(20)), ("segment", String(10)), ("paycode", String(40)),
            ("changes", Text), ("meta", Text), ("request_id", String(32)), ("hash", String(64)), ("prev_hash", String(64)),
        ],
    },
    "reports": {
        "label": "Report summaries (one row per day)",
        "key": ["report_date"],
        "default_table": "shiftkey_reports",
        "columns": [
            ("report_date", Date), ("company", String(120)), ("source", String(255)), ("source_kind", String(10)),
            ("source_sha256", String(64)), ("uploaded_at", DateTime), ("total", Integer), ("present", Integer),
            ("absent", Integer), ("halfday", Integer), ("mispunch", Integer), ("needs_review", Integer),
            ("pending", Integer), ("corrected", Integer), ("report_id", Integer), ("updated_at", DateTime),
        ],
    },
}


def dataset_catalog() -> List[Dict[str, Any]]:
    return [{"key": k, "label": v["label"], "key_fields": v["key"], "default_table": v["default_table"],
             "columns": [{"name": c, "type": t.__name__ if isinstance(t, type) else t.__class__.__name__}
                         for c, t in v["columns"]]} for k, v in DATASETS.items()]


# ----------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------

def _eff(rec: AttendanceRecord) -> Tuple[Optional[str], Optional[str], str, Optional[int]]:
    if rec.is_corrected:
        return rec.corrected_in, rec.corrected_out, (rec.corrected_status or rec.status or "").strip(), rec.corrected_hrs_minutes
    return rec.time_in, rec.time_out, (rec.status or "").strip(), rec.hrs_minutes


def attendance_rows(db: Session, reports: Sequence[Report]) -> List[Dict[str, Any]]:
    out = []
    for rep in reports:
        for r in rep.records:
            t_in, t_out, st, mins = _eff(r)
            last = [c for c in r.corrections if c.action != "revert"]
            out.append({
                "report_date": rep.report_date, "paycode": r.paycode, "segment": r.segment, "card_no": r.card_no,
                "name": r.name, "dept_code": r.dept_code, "dept_name": r.dept_name, "shift": r.shift,
                "shift_start": r.shift_start, "time_in": t_in, "time_out": t_out, "lunch_out": r.lunch_out,
                "lunch_in": r.lunch_in, "hrs": fmt_minutes(mins) if mins is not None else None, "hrs_minutes": mins,
                "status": st, "status_label": STATUS_LABELS.get(st, st), "machine_in": r.time_in,
                "machine_out": r.time_out, "machine_status": (r.status or "").strip(), "machine_hrs": r.hrs,
                "is_corrected": bool(r.is_corrected),
                "correction_action": last[-1].action if (r.is_corrected and last) else None,
                "correction_remark": r.correction_remark, "corrected_by": last[-1].created_by_name if (r.is_corrected and last) else None,
                "corrected_at": r.corrected_at, "is_halfday": st == "HLF",
                "is_mispunch": bool(r.is_mispunch) and not r.is_corrected,
                "needs_review": bool(r.has_warning) and not r.is_corrected, "is_night": bool(r.is_night),
                "early": r.early, "late": r.late, "shift_early": r.shift_early, "ot": r.ot, "manual": bool(r.manual),
                "source": rep.source_filename, "report_id": rep.id, "record_id": r.id, "updated_at": utcnow(),
            })
    return out


def corrections_rows(db: Session, reports: Sequence[Report]) -> List[Dict[str, Any]]:
    ids = [r.id for r in reports]
    rows = db.execute(select(Correction, AttendanceRecord, Report).join(AttendanceRecord, Correction.record_id == AttendanceRecord.id)
                      .join(Report, Correction.report_id == Report.id).where(Correction.report_id.in_(ids)).order_by(Correction.id)).all()
    return [{
        "correction_id": c.id, "report_date": rep.report_date, "paycode": rec.paycode, "name": rec.name,
        "dept_code": rec.dept_code, "dept_name": rec.dept_name, "shift": rec.shift, "action": c.action, "field": c.field,
        "original_in": c.original_in, "original_out": c.original_out, "original_status": c.original_status,
        "corrected_in": c.corrected_in, "corrected_out": c.corrected_out, "corrected_status": c.corrected_status,
        "corrected_hrs": fmt_minutes(c.corrected_hrs_minutes) if c.corrected_hrs_minutes is not None else None,
        "remark": c.remark, "created_by": c.created_by_name, "created_at": c.created_at, "record_id": c.record_id,
        "report_id": c.report_id,
    } for c, rec, rep in rows]


def audit_rows(db: Session, since: Optional[datetime] = None, until: Optional[datetime] = None) -> List[Dict[str, Any]]:
    import json
    stmt = select(AuditEvent).order_by(AuditEvent.id)
    if since:
        stmt = stmt.where(AuditEvent.ts >= since)
    if until:
        stmt = stmt.where(AuditEvent.ts < until)
    return [{
        "event_id": e.id, "ts": e.ts, "actor": e.actor_username, "actor_role": e.actor_role, "ip": e.ip,
        "action": e.action, "category": e.category, "severity": e.severity, "outcome": e.outcome, "summary": e.summary,
        "target_type": e.target_type, "target_id": e.target_id, "target_label": e.target_label,
        "report_date": e.report_date, "dept_code": e.dept_code, "segment": e.segment, "paycode": e.paycode,
        "changes": json.dumps(e.changes, default=str) if e.changes else None,
        "meta": json.dumps(e.meta, default=str) if e.meta else None,
        "request_id": e.request_id, "hash": e.hash, "prev_hash": e.prev_hash,
    } for e in db.scalars(stmt).all()]


def report_rows(db: Session, reports: Sequence[Report]) -> List[Dict[str, Any]]:
    out = []
    for rep in reports:
        recs = rep.records
        eff = [_eff(r)[2] for r in recs]
        out.append({
            "report_date": rep.report_date, "company": rep.company, "source": rep.source_filename,
            "source_kind": rep.source_kind or "upload", "source_sha256": rep.source_sha256, "uploaded_at": rep.uploaded_at,
            "total": len(recs), "present": sum(1 for s in eff if s == "P"), "absent": sum(1 for s in eff if s == "A"),
            "halfday": sum(1 for s in eff if s == "HLF"), "mispunch": sum(1 for r in recs if r.is_mispunch),
            "needs_review": sum(1 for r in recs if r.has_warning),
            "pending": sum(1 for r in recs if (r.is_mispunch or r.has_warning) and not r.is_corrected),
            "corrected": sum(1 for r in recs if r.is_corrected), "report_id": rep.id, "updated_at": utcnow(),
        })
    return out


def build_rows(db: Session, dataset: str, reports: Sequence[Report], since: Optional[datetime] = None,
               until: Optional[datetime] = None) -> List[Dict[str, Any]]:
    if dataset == "attendance":
        return attendance_rows(db, reports)
    if dataset == "corrections":
        return corrections_rows(db, reports)
    if dataset == "audit_events":
        return audit_rows(db, since, until)
    if dataset == "reports":
        return report_rows(db, reports)
    raise ValueError(f"Unknown dataset '{dataset}'.")


# ----------------------------------------------------------------------------
# Target table + upsert
# ----------------------------------------------------------------------------

def resolve_columns(dataset: str, column_map: Optional[Dict[str, str]]) -> List[Tuple[str, str, Any]]:
    """[(shiftkey_field, target_column, type)] – a column map restricts and renames fields."""
    spec = DATASETS[dataset]
    types = dict(spec["columns"])
    if column_map:
        return [(f, t or f, types[f]) for f, t in column_map.items() if f in types and t not in (None, "")]
    return [(f, f, t) for f, t in spec["columns"]]


def ensure_table(engine, table: str, schema: Optional[str], columns: List[Tuple[str, str, Any]],
                 key_targets: List[str], create: bool) -> Table:
    md = MetaData()
    insp = inspect(engine)
    if insp.has_table(table, schema=schema):
        return Table(table, md, autoload_with=engine, schema=schema)
    if not create:
        raise RuntimeError(f"Target table '{table}' does not exist and 'create table' is off.")
    cols = []
    for _, target, typ in columns:
        cols.append(Column(target, typ, primary_key=target in key_targets, nullable=target not in key_targets))
    if not any(c.primary_key for c in cols):
        cols.insert(0, Column("id", Integer, primary_key=True, autoincrement=True))
    t = Table(table, md, *cols, schema=schema)
    md.create_all(engine)
    return t


def push_rows(conn: Connection, dataset: str, rows: List[Dict[str, Any]], table_name: str, schema: Optional[str],
              column_map: Optional[Dict[str, str]], key_fields: Optional[List[str]], create: bool,
              mode: str = "upsert") -> Dict[str, Any]:
    if conn.read_only:
        raise RuntimeError(f"Connection '{conn.name}' is marked read-only. Allow writes on the connection first.")
    columns = resolve_columns(dataset, column_map)
    field_to_target = {f: t for f, t, _ in columns}
    keys = key_fields or DATASETS[dataset]["key"]
    missing = [k for k in keys if k not in field_to_target]
    if missing:
        raise RuntimeError(f"Key field(s) {', '.join(missing)} are not part of the column map.")
    key_targets = [field_to_target[k] for k in keys]
    engine = make_engine(conn)
    table = ensure_table(engine, table_name, schema, columns, key_targets, create)
    present = {c.name for c in table.columns}
    unknown = [t for _, t, _ in columns if t not in present]
    if unknown:
        raise RuntimeError(f"Target table '{table_name}' has no column(s): {', '.join(unknown)}")
    inserted = updated = skipped = 0
    with engine.begin() as c:
        for row in rows:
            payload = {t: row.get(f) for f, t, _ in columns}
            cond = and_(*[table.c[t] == payload[t] for t in key_targets])
            did = 0
            if mode in ("upsert", "update"):
                values = {t: v for t, v in payload.items() if t not in key_targets}
                if values:
                    did = c.execute(update(table).where(cond).values(**values)).rowcount or 0
                else:
                    did = 1 if c.execute(select(table.c[key_targets[0]]).where(cond)).first() else 0
                if did:
                    updated += did
                    continue
                if mode == "update":
                    skipped += 1
                    continue
            if mode == "insert" and c.execute(select(table.c[key_targets[0]]).where(cond)).first():
                skipped += 1
                continue
            c.execute(insert(table).values(**payload))
            inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "rows": len(rows),
            "table": (schema + "." if schema else "") + table_name}
