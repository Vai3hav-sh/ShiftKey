"""SQL integration endpoints (admin only): connections, console, jobs, runs."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import audit
from .auth import require_admin
from .connectors import engine as ext
from .connectors import jobs as sync
from .connectors.crypto import decrypt, encrypt
from .connectors.push import DATASETS, dataset_catalog
from .connectors.transform import FIELDS, PUNCH_FIELDS, suggest_mapping
from .db import get_session
from .models import Connection, SyncJob, SyncRun, User, utcnow
from .schemas import ConnectionIn, PreviewIn, QueryIn, RunIn, SyncJobIn

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])


def _conn(db: Session, cid: int) -> Connection:
    c = db.scalar(select(Connection).options(selectinload(Connection.jobs)).where(Connection.id == cid))
    if c is None:
        raise HTTPException(404, "Connection not found.")
    return c


def _job(db: Session, jid: int) -> SyncJob:
    j = db.scalar(select(SyncJob).options(selectinload(SyncJob.connection)).where(SyncJob.id == jid))
    if j is None:
        raise HTTPException(404, "Sync job not found.")
    return j


def _conn_snapshot(c: Connection) -> Dict[str, Any]:
    return {"name": c.name, "dialect": c.dialect, "host": c.host, "port": c.port, "database": c.database,
            "username": c.username, "options": c.options or {}, "read_only": c.read_only,
            "has_password": bool(c.password_enc), "has_url": bool(c.url_enc), "notes": c.notes}


def _job_snapshot(j: SyncJob) -> Dict[str, Any]:
    d = sync.job_dict(j)
    for k in ("id", "connection_name", "last_run_at", "last_status", "last_message", "created_at", "updated_at", "schedule_last_date"):
        d.pop(k, None)
    return d


def _parse_date(s: Optional[str], default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"Invalid date '{s}' (use YYYY-MM-DD).")


# ----------------------------------------------------------------------------
# Catalogues
# ----------------------------------------------------------------------------

@router.get("/sync/catalog")
def api_catalog():
    return {"dialects": ext.dialect_catalog(), "fields": FIELDS, "punch_fields": PUNCH_FIELDS,
            "datasets": dataset_catalog(), "params": list(ext.PARAM_NAMES),
            "push_modes": [{"key": "upsert", "label": "Insert new, update existing"},
                           {"key": "update", "label": "Update existing only"},
                           {"key": "insert", "label": "Insert new only"}]}


# ----------------------------------------------------------------------------
# Connections
# ----------------------------------------------------------------------------

@router.get("/connections")
def api_connections(db: Session = Depends(get_session)):
    rows = db.scalars(select(Connection).options(selectinload(Connection.jobs)).order_by(Connection.name)).all()
    return [sync.connection_dict(c) for c in rows]


def _apply_connection(c: Connection, body: ConnectionIn) -> None:
    if body.dialect not in ext.DIALECTS:
        raise HTTPException(400, "Unknown dialect.")
    c.name = body.name.strip()
    c.dialect = body.dialect
    c.host = (body.host or "").strip() or None
    c.port = body.port
    c.database = (body.database or "").strip() or None
    c.username = (body.username or "").strip() or None
    if body.password is not None:
        c.password_enc = encrypt(body.password) if body.password else None
    if body.url is not None:
        c.url_enc = encrypt(body.url.strip()) if body.url.strip() else None
    c.options = {k: v for k, v in (body.options or {}).items() if str(k).strip()}
    c.read_only = body.read_only
    c.notes = (body.notes or "").strip() or None
    if c.dialect != "sqlite" and not c.url_enc and not c.host:
        raise HTTPException(400, "Host is required (or provide a full connection URL).")


@router.post("/connections", status_code=201)
def api_create_connection(body: ConnectionIn, request: Request, admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)):
    if db.scalar(select(Connection).where(Connection.name == body.name.strip())):
        raise HTTPException(409, "A connection with that name exists.")
    c = Connection()
    _apply_connection(c, body)
    db.add(c)
    db.commit()
    db.refresh(c)
    audit.record(db, "connection.created", f"{admin.username} added SQL connection '{c.name}' ({ext.DIALECTS[c.dialect]['label']}"
                 f"{', ' + c.host if c.host else ''}{', db ' + c.database if c.database else ''})",
                 request=request, actor=admin, target_type="connection", target_id=c.id, target_label=c.name, after=_conn_snapshot(c))
    return sync.connection_dict(c)


@router.patch("/connections/{cid}")
def api_update_connection(cid: int, body: ConnectionIn, request: Request, admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)):
    c = _conn(db, cid)
    other = db.scalar(select(Connection).where(Connection.name == body.name.strip(), Connection.id != cid))
    if other:
        raise HTTPException(409, "A connection with that name exists.")
    before = _conn_snapshot(c)
    _apply_connection(c, body)
    c.updated_at = utcnow()
    db.commit()
    db.refresh(c)
    after = _conn_snapshot(c)
    changes = audit.diff(before, after)
    audit.record(db, "connection.updated", f"{admin.username} changed connection '{c.name}'" +
                 (": " + ", ".join(f"{x['field']} {x['from']} → {x['to']}" for x in changes if x['field'] not in ('has_password',))[:250] if changes else "")
                 + (" (password updated)" if body.password else ""),
                 request=request, actor=admin, target_type="connection", target_id=c.id, target_label=c.name, before=before, after=after)
    return sync.connection_dict(c)


@router.delete("/connections/{cid}", status_code=204)
def api_delete_connection(cid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, cid)
    before = _conn_snapshot(c)
    name, jobs = c.name, len(c.jobs)
    db.delete(c)
    db.commit()
    audit.record(db, "connection.deleted", f"{admin.username} deleted connection '{name}'" + (f" and its {jobs} job(s)" if jobs else ""),
                 request=request, actor=admin, target_type="connection", target_id=cid, target_label=name, before=before)
    return {}


@router.post("/connections/test")
def api_test_unsaved(body: ConnectionIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    """Test the settings in the form before saving. ``password`` empty + ``id`` unknown → fails naturally."""
    c = Connection()
    try:
        _apply_connection(c, body)
    except HTTPException as exc:
        return {"ok": False, "message": exc.detail}
    result = ext.test_connection(c, body.password or "")
    audit.record(db, "connection.tested", f"{admin.username} tested connection settings '{c.name}': {result['message']}",
                 request=request, actor=admin, outcome="success" if result["ok"] else "failure",
                 target_type="connection", target_label=c.name, meta={k: v for k, v in result.items() if k != "message"})
    return result


@router.post("/connections/{cid}/test")
def api_test_connection(cid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, cid)
    result = ext.test_connection(c)
    c.last_test_at = utcnow()
    c.last_test_ok = result["ok"]
    c.last_test_message = result["message"]
    if result.get("server"):
        c.server_info = result["server"][:300]
    db.commit()
    audit.record(db, "connection.tested", f"{admin.username} tested connection '{c.name}': {result['message']}",
                 request=request, actor=admin, outcome="success" if result["ok"] else "failure",
                 target_type="connection", target_id=c.id, target_label=c.name, meta={k: v for k, v in result.items() if k != "message"})
    return result


@router.get("/connections/{cid}/schema")
def api_schema(cid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, cid)
    try:
        tables = ext.list_tables(c)
    except Exception as exc:
        raise HTTPException(502, ext._friendly(exc))
    audit.record(db, "sql.schema", f"{admin.username} browsed the schema of '{c.name}' ({len(tables)} tables)",
                 request=request, actor=admin, target_type="connection", target_id=c.id, target_label=c.name)
    return tables


@router.get("/connections/{cid}/schema/{table}")
def api_table(cid: int, table: str, schema: Optional[str] = None, preview: bool = False,
              limit: int = Query(50, ge=1, le=500), admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, cid)
    try:
        out: Dict[str, Any] = {"columns": ext.table_columns(c, table, schema)}
        if preview:
            out["preview"] = ext.preview_table(c, table, schema, limit)
    except Exception as exc:
        raise HTTPException(502, ext._friendly(exc))
    return out


@router.post("/connections/{cid}/query")
def api_query(cid: int, body: QueryIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, cid)
    params = dict(body.params or {})
    for k in ext.PARAM_NAMES:
        if isinstance(params.get(k), str):
            try:
                params[k] = date.fromisoformat(params[k])
            except ValueError:
                pass
    try:
        result = ext.run_select(c, body.sql, params, body.limit)
    except ValueError as exc:
        audit.record(db, "sql.query", f"{admin.username}: query on '{c.name}' rejected – {exc}", request=request, actor=admin,
                     outcome="denied", target_type="connection", target_id=c.id, target_label=c.name, meta={"sql": body.sql[:2000]})
        raise HTTPException(400, str(exc))
    except Exception as exc:
        msg = ext._friendly(exc)
        audit.record(db, "sql.query", f"{admin.username}: query on '{c.name}' failed – {msg}", request=request, actor=admin,
                     outcome="failure", target_type="connection", target_id=c.id, target_label=c.name, meta={"sql": body.sql[:2000]})
        raise HTTPException(502, msg)
    audit.record(db, "sql.query", f"{admin.username} ran a query on '{c.name}' ({result['row_count']} rows"
                 f"{'+' if result['truncated'] else ''}, {result['elapsed_ms']} ms)",
                 request=request, actor=admin, target_type="connection", target_id=c.id, target_label=c.name,
                 meta={"sql": body.sql[:2000], "rows": result["row_count"], "elapsed_ms": result["elapsed_ms"],
                       "params": {k: str(v) for k, v in params.items()}})
    result["suggested_mapping"] = suggest_mapping(result["columns"])
    return result


# ----------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------

def _apply_job(j: SyncJob, body: SyncJobIn, db: Session) -> None:
    if body.direction not in ("pull", "push"):
        raise HTTPException(400, "Direction must be pull or push.")
    conn = db.get(Connection, body.connection_id)
    if conn is None:
        raise HTTPException(400, "Connection not found.")
    j.name = body.name.strip()
    j.direction = body.direction
    j.connection_id = conn.id
    j.enabled = body.enabled
    j.description = (body.description or "").strip() or None
    if body.direction == "pull":
        if body.mode not in ("daily", "punches"):
            raise HTTPException(400, "Pull mode must be daily or punches.")
        if not (body.sql or "").strip():
            raise HTTPException(400, "The SQL query is required.")
        try:
            ext.validate_readonly(body.sql)
            if body.employees_sql:
                ext.validate_readonly(body.employees_sql)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if body.mode == "punches" and not (body.employees_sql or "").strip():
            raise HTTPException(400, "Punch-log mode needs an employee master query.")
        j.mode, j.sql, j.employees_sql = body.mode, body.sql.strip(), (body.employees_sql or "").strip() or None
        j.mapping = {k: v for k, v in (body.mapping or {}).items() if v}
        j.employees_mapping = {k: v for k, v in (body.employees_mapping or {}).items() if v}
        j.options = body.options or {}
        j.dataset = j.target_table = None
    else:
        if body.dataset not in DATASETS:
            raise HTTPException(400, "Unknown dataset.")
        if body.push_mode not in ("upsert", "update", "insert"):
            raise HTTPException(400, "Unknown push mode.")
        if conn.read_only:
            raise HTTPException(400, f"Connection '{conn.name}' is read-only. Allow writes on it before pushing.")
        j.dataset = body.dataset
        j.target_table = (body.target_table or "").strip() or DATASETS[body.dataset]["default_table"]
        j.target_schema = (body.target_schema or "").strip() or None
        j.key_fields = body.key_fields or None
        j.column_map = {k: v for k, v in (body.column_map or {}).items() if v} or None
        j.create_table = body.create_table
        j.push_mode = body.push_mode
        j.auto_push = body.auto_push
        j.mode = j.sql = j.employees_sql = None
        j.mapping = j.employees_mapping = None
        j.options = body.options or {}
    j.schedule_enabled = body.schedule_enabled
    j.schedule_time = body.schedule_time
    j.schedule_offset_days = body.schedule_offset_days
    if j.schedule_enabled and not j.schedule_time:
        raise HTTPException(400, "Set a time for the schedule.")


@router.get("/sync/jobs")
def api_jobs(db: Session = Depends(get_session)):
    rows = db.scalars(select(SyncJob).options(selectinload(SyncJob.connection)).order_by(SyncJob.direction, SyncJob.name)).all()
    return [sync.job_dict(j) for j in rows]


@router.post("/sync/jobs", status_code=201)
def api_create_job(body: SyncJobIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    if db.scalar(select(SyncJob).where(SyncJob.name == body.name.strip())):
        raise HTTPException(409, "A job with that name exists.")
    j = SyncJob()
    _apply_job(j, body, db)
    db.add(j)
    db.commit()
    j = _job(db, j.id)
    what = f"{j.mode} pull from" if j.direction == "pull" else f"push of {j.dataset} to"
    audit.record(db, "sync.job.created", f"{admin.username} created sync job '{j.name}' ({what} {j.connection.name}"
                 f"{', daily at ' + j.schedule_time if j.schedule_enabled else ''})",
                 request=request, actor=admin, target_type="sync_job", target_id=j.id, target_label=j.name, after=_job_snapshot(j))
    return sync.job_dict(j)


@router.patch("/sync/jobs/{jid}")
def api_update_job(jid: int, body: SyncJobIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    j = _job(db, jid)
    if db.scalar(select(SyncJob).where(SyncJob.name == body.name.strip(), SyncJob.id != jid)):
        raise HTTPException(409, "A job with that name exists.")
    before = _job_snapshot(j)
    _apply_job(j, body, db)
    j.updated_at = utcnow()
    db.commit()
    j = _job(db, jid)
    after = _job_snapshot(j)
    changes = audit.diff(before, after)
    audit.record(db, "sync.job.updated", f"{admin.username} changed sync job '{j.name}'" +
                 (": " + ", ".join(c["field"] for c in changes) if changes else ""),
                 request=request, actor=admin, target_type="sync_job", target_id=j.id, target_label=j.name, before=before, after=after)
    return sync.job_dict(j)


@router.delete("/sync/jobs/{jid}", status_code=204)
def api_delete_job(jid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    j = _job(db, jid)
    before = _job_snapshot(j)
    name = j.name
    db.delete(j)
    db.commit()
    audit.record(db, "sync.job.deleted", f"{admin.username} deleted sync job '{name}'", request=request, actor=admin,
                 target_type="sync_job", target_id=jid, target_label=name, before=before)
    return {}


@router.post("/sync/preview")
def api_preview(body: PreviewIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    c = _conn(db, body.connection_id)
    d = _parse_date(body.date, date.today() - timedelta(days=1))
    try:
        ext.validate_readonly(body.sql)
        if body.employees_sql:
            ext.validate_readonly(body.employees_sql)
        out = sync.fetch_pull_preview(c, body.model_dump(), d, body.limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, ext._friendly(exc))
    audit.record(db, "sql.query", f"{admin.username} previewed a pull from '{c.name}' for {d.strftime('%d %b %Y')} "
                 f"({out['main']['row_count']} rows)", request=request, actor=admin, target_type="connection",
                 target_id=c.id, target_label=c.name, report_date=d, meta={"sql": body.sql[:2000], "mode": body.mode})
    return out


@router.post("/sync/jobs/{jid}/run")
def api_run_job(jid: int, body: RunIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    j = _job(db, jid)
    yesterday = date.today() - timedelta(days=j.schedule_offset_days if j.schedule_offset_days is not None else 1)
    d_from = _parse_date(body.date_from, yesterday)
    d_to = _parse_date(body.date_to, d_from)
    try:
        if j.direction == "pull":
            run = sync.run_pull(db, j, d_from, d_to, trigger="manual", actor=admin, request=request)
        else:
            run = sync.run_push(db, j, d_from, d_to, trigger="manual", actor=admin, request=request)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return sync.run_dict(run)


@router.get("/sync/runs")
def api_runs(job_id: Optional[int] = None, limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_session)):
    stmt = select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)
    if job_id:
        stmt = stmt.where(SyncRun.job_id == job_id)
    return [sync.run_dict(r) for r in db.scalars(stmt).all()]


@router.post("/sync/tick")
def api_tick(db: Session = Depends(get_session)):
    """Run whatever is due right now (handy for testing a schedule)."""
    runs = sync.run_scheduled(db)
    return [sync.run_dict(r) for r in runs]
