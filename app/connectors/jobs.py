"""Run pull / push jobs, keep run history, scheduler and auto-push."""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import audit
from ..config import settings
from ..db import SessionLocal
from ..models import Connection, Report, SyncJob, SyncRun, User, utcnow
from ..parser import ParsedReport
from ..services import save_report
from . import engine as ext
from .push import DATASETS, build_rows, push_rows
from .transform import punches_to_reports, rows_to_csv_bytes, rows_to_reports

log = logging.getLogger("shiftkey.sync")
_run_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def date_range(d_from: date, d_to: date) -> List[date]:
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > 92:
        raise ValueError("Please run at most 93 days at a time.")
    return [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]


def _params_for(d_from: date, d_to: date, opts: Dict[str, Any]) -> Dict[str, Any]:
    fmt = (opts or {}).get("date_param_format")
    conv = (lambda d: d.strftime(fmt)) if fmt else (lambda d: d)
    return {"report_date": conv(d_from), "date_from": conv(d_from), "date_to": conv(d_to)}


def _new_run(db: Session, job: SyncJob, trigger: str, params: Dict[str, Any], actor: Optional[User]) -> SyncRun:
    run = SyncRun(job_id=job.id, job_name=job.name, direction=job.direction, trigger=trigger, params=params,
                  actor_id=actor.id if actor else None, actor_username=actor.username if actor else None, log=[])
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _finish(db: Session, run: SyncRun, job: SyncJob, status: str, message: str, **counts) -> SyncRun:
    run.status = status
    run.message = message[:4000]
    run.finished_at = utcnow()
    for k, v in counts.items():
        setattr(run, k, v)
    job.last_run_at = run.finished_at
    job.last_status = status
    job.last_message = message[:400]
    db.commit()
    db.refresh(run)
    return run


def _log(run: SyncRun, msg: str) -> None:
    run.log = (run.log or []) + [f"{utcnow().strftime('%H:%M:%S')} {msg}"]


# ----------------------------------------------------------------------------
# Pull
# ----------------------------------------------------------------------------

def fetch_pull_preview(conn: Connection, job_like: Dict[str, Any], d: date, limit: int) -> Dict[str, Any]:
    """Run the job's queries for one day, return columns, sample rows, suggested mapping and the parsed summary."""
    from .transform import suggest_mapping
    opts = job_like.get("options") or {}
    params = _params_for(d, d, opts)
    out: Dict[str, Any] = {"date": d.isoformat()}
    main = ext.run_select(conn, job_like.get("sql") or "", params, limit)
    out["main"] = main
    out["suggested_mapping"] = suggest_mapping(main["columns"], punch_mode=(job_like.get("mode") == "punches"))
    if job_like.get("mode") == "punches" and job_like.get("employees_sql"):
        emp = ext.run_select(conn, job_like["employees_sql"], params, limit)
        out["employees"] = emp
        out["suggested_employees_mapping"] = suggest_mapping(emp["columns"])
    mapping = job_like.get("mapping") or out["suggested_mapping"]
    try:
        reports = _pull_reports(conn, job_like, [d], mapping, job_like.get("employees_mapping") or out.get("suggested_employees_mapping") or {})
        out["parsed"] = [{"report_date": r.report_date.isoformat() if r.report_date else None, "summary": r.summary(),
                          "problems": r.problems,
                          "flagged": [{"paycode": x.paycode, "name": x.name, "dept": x.dept_name, "in": x.time_in,
                                       "out": x.time_out, "hrs": x.hrs, "status": x.status, "notes": x.notes}
                                      for x in r.records if x.is_mispunch or x.is_halfday or x.has_warning][:25],
                          "sample": [{"paycode": x.paycode, "name": x.name, "dept": x.dept_name, "shift": x.shift,
                                      "in": x.time_in, "out": x.time_out, "hrs": x.hrs, "status": x.status}
                                     for x in r.records[:8]]} for r in reports]
        out["parse_error"] = None
    except Exception as exc:
        out["parsed"] = []
        out["parse_error"] = str(exc)
    return out


def _pull_reports(conn: Connection, job: Any, dates: List[date], mapping: Dict[str, str],
                  employees_mapping: Dict[str, str]) -> List[ParsedReport]:
    g = (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, None))
    opts = g("options") or {}
    name = g("name") or "preview"
    label = f"sql://{conn.name}/{name}"
    mode = g("mode") or "daily"
    if not mapping.get("paycode"):
        raise ValueError("Map the PayCode column before running the job.")
    if mode == "punches":
        if not g("employees_sql"):
            raise ValueError("Punch-log mode needs an employee master query.")
        if not mapping.get("punch_time"):
            raise ValueError("Map the punch date-time column.")
        # punches of a night shift land on the next calendar day: ask for one day before and two after
        params = _params_for(dates[0] - timedelta(days=1), dates[-1] + timedelta(days=2), opts)
        employees = ext.fetch_all(conn, g("employees_sql"), params)
        punches = ext.fetch_all(conn, g("sql"), params)
        return punches_to_reports(employees, employees_mapping, punches, mapping, opts, dates, label)
    sql = g("sql") or ""
    refs = ext.referenced_params(sql)
    reports: List[ParsedReport] = []
    if "report_date" in refs or (not refs and not mapping.get("report_date")):
        for d in dates:
            rows = ext.fetch_all(conn, sql, _params_for(d, d, opts))
            reports.extend(rows_to_reports(rows, mapping, opts, d, label))
    else:
        rows = ext.fetch_all(conn, sql, _params_for(dates[0], dates[-1], opts))
        wanted = set(dates)
        reports = [r for r in rows_to_reports(rows, mapping, opts, dates[0] if len(dates) == 1 else None, label)
                   if r.report_date in wanted or (r.report_date is None and len(dates) == 1)]
        for r in reports:
            if r.report_date is None:
                r.report_date = dates[0]
    return reports


def run_pull(db: Session, job: SyncJob, d_from: date, d_to: date, trigger: str = "manual",
             actor: Optional[User] = None, request=None) -> SyncRun:
    dates = date_range(d_from, d_to)
    run = _new_run(db, job, trigger, {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()}, actor)
    conn = job.connection
    created: List[int] = []
    rows_in = 0
    try:
        with _run_lock:
            reports = _pull_reports(conn, job, dates, job.mapping or {}, job.employees_mapping or {})
            opts = job.options or {}
            replace = opts.get("replace_existing", True)
            for rep in reports:
                rows_in += rep.total
                raw_rows = [{"raw": r.raw} for r in rep.records]
                source = rows_to_csv_bytes(raw_rows, ["raw"]) if raw_rows else b""
                try:
                    report, replaced, previous = save_report(db, rep, source, replace=replace)
                except FileExistsError:
                    _log(run, f"{rep.report_date}: already imported, skipped (replace is off)")
                    continue
                report.source_kind = "sql"
                report.sync_job_id = job.id
                report.sync_run_id = run.id
                db.commit()
                created.append(report.id)
                _log(run, f"{rep.report_date}: {rep.total} employees, {rep.count('is_mispunch')} mispunch, "
                          f"{rep.count('is_halfday')} half day{' (replaced previous import)' if replaced else ''}")
                audit.record(db, "sync.pull", f"{(actor.username if actor else 'scheduler')} pulled {rep.report_date.strftime('%d %b %Y')} "
                             f"from {conn.name} via job '{job.name}' ({rep.total} employees{', replaced' if replaced else ''})",
                             request=request, actor=actor, actor_username=None if actor else "scheduler",
                             target_type="report", target_id=report.id, target_label=rep.report_date.strftime("%d %b %Y"),
                             report_id=report.id, report_date=rep.report_date, before=previous,
                             after={"total": rep.total, "source": rep.source_filename, "sha256": report.source_sha256},
                             meta={"job_id": job.id, "job": job.name, "connection": conn.name, "run_id": run.id, "trigger": trigger})
            if not reports:
                _log(run, "query returned no rows for the selected day(s)")
        status = "success" if created or not dates else "partial"
        msg = f"{len(created)} day(s) imported, {rows_in} employee rows" if created else "No data found for the selected day(s)."
        run = _finish(db, run, job, status if created else "partial", msg, rows_in=rows_in, rows_out=len(created), report_ids=created)
        if created and settings.sync_async:
            trigger_auto_push(created)
        elif created:
            auto_push_now(db, created, trigger="auto")
        return run
    except Exception as exc:
        log.exception("pull job %s failed", job.name)
        _log(run, f"ERROR {exc}")
        run = _finish(db, run, job, "failed", f"{exc.__class__.__name__}: {exc}", rows_in=rows_in, report_ids=created)
        audit.record(db, "sync.pull", f"Pull job '{job.name}' failed: {exc}", request=request, actor=actor,
                     actor_username=None if actor else "scheduler", outcome="failure",
                     meta={"job_id": job.id, "run_id": run.id, "error": traceback.format_exc()[-2000:]}, severity="warning")
        return run


# ----------------------------------------------------------------------------
# Push
# ----------------------------------------------------------------------------

def _reports_between(db: Session, d_from: date, d_to: date) -> List[Report]:
    return db.scalars(select(Report).options(selectinload(Report.records)).where(Report.report_date >= d_from, Report.report_date <= d_to)
                      .order_by(Report.report_date)).all()


def run_push(db: Session, job: SyncJob, d_from: date, d_to: date, trigger: str = "manual",
             actor: Optional[User] = None, request=None, report_ids: Optional[List[int]] = None) -> SyncRun:
    run = _new_run(db, job, trigger, {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "report_ids": report_ids}, actor)
    conn = job.connection
    try:
        if report_ids:
            reports = db.scalars(select(Report).options(selectinload(Report.records)).where(Report.id.in_(report_ids))).all()
        else:
            reports = _reports_between(db, d_from, d_to)
        for rep in reports:
            for r in rep.records:
                _ = r.corrections  # ensure loaded for attendance rows
        since = datetime.combine(d_from, datetime.min.time()) if job.dataset == "audit_events" else None
        until = datetime.combine(d_to + timedelta(days=1), datetime.min.time()) if job.dataset == "audit_events" else None
        rows = build_rows(db, job.dataset, reports, since, until)
        table = job.target_table or DATASETS[job.dataset]["default_table"]
        with _run_lock:
            result = push_rows(conn, job.dataset, rows, table, job.target_schema or None, job.column_map or None,
                               job.key_fields or None, job.create_table, job.push_mode or "upsert")
        msg = (f"{result['rows']} rows → {result['table']}: {result['inserted']} inserted, "
               f"{result['updated']} updated, {result['skipped']} skipped")
        _log(run, msg)
        run = _finish(db, run, job, "success", msg, rows_in=len(rows), rows_out=result["inserted"] + result["updated"],
                      report_ids=[r.id for r in reports])
        audit.record(db, "sync.push", f"{(actor.username if actor else trigger)} pushed {job.dataset} "
                     f"{d_from.strftime('%d %b')}–{d_to.strftime('%d %b %Y')} to {conn.name}.{result['table']} "
                     f"({result['inserted']} inserted, {result['updated']} updated)",
                     request=request, actor=actor, actor_username=None if actor else trigger,
                     meta={"job_id": job.id, "job": job.name, "connection": conn.name, "run_id": run.id, **result, "trigger": trigger})
        return run
    except Exception as exc:
        log.exception("push job %s failed", job.name)
        _log(run, f"ERROR {exc}")
        run = _finish(db, run, job, "failed", f"{exc.__class__.__name__}: {exc}")
        audit.record(db, "sync.push", f"Push job '{job.name}' failed: {exc}", request=request, actor=actor,
                     actor_username=None if actor else trigger, outcome="failure",
                     meta={"job_id": job.id, "run_id": run.id, "error": traceback.format_exc()[-2000:]}, severity="warning")
        return run


# ----------------------------------------------------------------------------
# Auto-push (after imports / corrections)
# ----------------------------------------------------------------------------

_push_queue: "queue.Queue[List[int]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_stop = threading.Event()


def auto_push_now(db: Session, report_ids: List[int], trigger: str = "auto") -> List[SyncRun]:
    jobs = db.scalars(select(SyncJob).options(selectinload(SyncJob.connection))
                      .where(SyncJob.direction == "push", SyncJob.enabled == True, SyncJob.auto_push == True)).all()  # noqa: E712
    if not jobs:
        return []
    reports = db.scalars(select(Report).where(Report.id.in_(report_ids))).all()
    if not reports:
        return []
    d_from, d_to = min(r.report_date for r in reports), max(r.report_date for r in reports)
    return [run_push(db, j, d_from, d_to, trigger=trigger, report_ids=report_ids) for j in jobs]


def trigger_auto_push(report_ids: List[int]) -> None:
    if not settings.sync_async:
        with SessionLocal() as db:
            auto_push_now(db, report_ids)
        return
    _push_queue.put(list(report_ids))


def _worker_loop() -> None:  # pragma: no cover - background thread
    while not _stop.is_set():
        try:
            ids = _push_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            with SessionLocal() as db:
                auto_push_now(db, ids)
        except Exception:
            log.exception("auto-push failed")


# ----------------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------------

def due_jobs(db: Session, now: datetime) -> List[SyncJob]:
    jobs = db.scalars(select(SyncJob).options(selectinload(SyncJob.connection))
                      .where(SyncJob.enabled == True, SyncJob.schedule_enabled == True)).all()  # noqa: E712
    out = []
    for j in jobs:
        if not j.schedule_time:
            continue
        hh, mm = j.schedule_time.split(":")
        due_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now >= due_at and j.schedule_last_date != now.date():
            out.append(j)
    return out


def run_scheduled(db: Session, now: Optional[datetime] = None) -> List[SyncRun]:
    now = now or datetime.now()
    runs = []
    for j in due_jobs(db, now):
        target = now.date() - timedelta(days=j.schedule_offset_days or 0)
        j.schedule_last_date = now.date()
        db.commit()
        if j.direction == "pull":
            runs.append(run_pull(db, j, target, target, trigger="schedule"))
        else:
            runs.append(run_push(db, j, target, target, trigger="schedule"))
    return runs


def _scheduler_loop() -> None:  # pragma: no cover - background thread
    while not _stop.wait(30):
        try:
            with SessionLocal() as db:
                run_scheduled(db)
        except Exception:
            log.exception("scheduler tick failed")


def start_background() -> None:
    global _worker
    if not settings.scheduler_enabled or _worker is not None:
        return
    _stop.clear()
    _worker = threading.Thread(target=_worker_loop, name="shiftkey-autopush", daemon=True)
    _worker.start()
    threading.Thread(target=_scheduler_loop, name="shiftkey-scheduler", daemon=True).start()
    log.info("background scheduler started")


def stop_background() -> None:
    _stop.set()


# ----------------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------------

def connection_dict(c: Connection) -> Dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "dialect": c.dialect, "dialect_label": ext.DIALECTS.get(c.dialect, {}).get("label", c.dialect),
        "host": c.host, "port": c.port, "database": c.database, "username": c.username,
        "has_password": bool(c.password_enc), "has_url": bool(c.url_enc), "options": c.options or {},
        "read_only": c.read_only, "notes": c.notes, "driver_available": ext.driver_available(c.dialect),
        "created_at": c.created_at.isoformat() + "Z", "updated_at": c.updated_at.isoformat() + "Z",
        "last_test_at": c.last_test_at.isoformat() + "Z" if c.last_test_at else None,
        "last_test_ok": c.last_test_ok, "last_test_message": c.last_test_message, "server_info": c.server_info,
        "jobs": len(c.jobs) if c.jobs is not None else 0,
    }


def job_dict(j: SyncJob) -> Dict[str, Any]:
    return {
        "id": j.id, "name": j.name, "direction": j.direction, "connection_id": j.connection_id,
        "connection_name": j.connection.name if j.connection else None, "enabled": j.enabled, "description": j.description,
        "mode": j.mode, "sql": j.sql, "employees_sql": j.employees_sql, "mapping": j.mapping or {},
        "employees_mapping": j.employees_mapping or {}, "options": j.options or {},
        "dataset": j.dataset, "target_table": j.target_table, "target_schema": j.target_schema,
        "key_fields": j.key_fields or (DATASETS[j.dataset]["key"] if j.dataset in DATASETS else []),
        "column_map": j.column_map, "create_table": j.create_table, "push_mode": j.push_mode, "auto_push": j.auto_push,
        "schedule_enabled": j.schedule_enabled, "schedule_time": j.schedule_time,
        "schedule_offset_days": j.schedule_offset_days,
        "schedule_last_date": j.schedule_last_date.isoformat() if j.schedule_last_date else None,
        "last_run_at": j.last_run_at.isoformat() + "Z" if j.last_run_at else None,
        "last_status": j.last_status, "last_message": j.last_message,
        "created_at": j.created_at.isoformat() + "Z", "updated_at": j.updated_at.isoformat() + "Z",
    }


def run_dict(r: SyncRun) -> Dict[str, Any]:
    return {
        "id": r.id, "job_id": r.job_id, "job_name": r.job_name, "direction": r.direction, "trigger": r.trigger,
        "status": r.status, "started_at": r.started_at.isoformat() + "Z",
        "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None,
        "duration_ms": int((r.finished_at - r.started_at).total_seconds() * 1000) if r.finished_at else None,
        "params": r.params or {}, "rows_in": r.rows_in, "rows_out": r.rows_out, "report_ids": r.report_ids or [],
        "message": r.message, "log": r.log or [], "actor": r.actor_username,
    }
