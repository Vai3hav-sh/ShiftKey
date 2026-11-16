"""ShiftKey HTTP API + static web UI."""
from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from . import __version__, audit
from .connectors import jobs as sync_jobs
from .routes_sync import router as sync_router
from .auth import (
    _load_user, check_throttle, clear_failures, create_session, current_user, destroy_session,
    ensure_admin, hash_password, is_locked, note_failure, password_ok_for, require_admin,
    require_corrector, revoke_user_sessions, validate_password, verify_password,
)
from .config import SEGMENT_LABELS, settings
from .db import MIGRATED, SessionLocal, engine, get_session, init_db
from .exporter import build_csv, build_workbook, export_filename, workbook_bytes
from .models import (
    ROLE_ADMIN, ROLE_HOD, ROLE_HR, ROLES, AttendanceRecord, AuditEvent, Correction, Report, Shift,
    User, UserDepartment, UserSession, utcnow,
)
from .parser import ParseError, parse_report
from .schemas import CorrectionIn, LoginIn, PasswordChangeIn, ShiftIn, UserCreateIn, UserUpdateIn
from .services import (
    Scope, apply_correction, backfill, correction_dict, delete_report, get_report, list_corrections,
    list_departments, list_reports, list_sessions, list_shifts, me_dict, record_dict, record_snapshot,
    report_detail, report_snapshot, revert_correction, save_report, seed_shifts, shift_dict,
    user_dict, visible_records,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as db:
        outcome = ensure_admin(db)
        seed_shifts(db)
        backfill(db)
        if MIGRATED:
            audit.record(db, "system.migrated", f"Database schema updated: {', '.join(MIGRATED)}",
                         meta={"columns": list(MIGRATED)})
        if outcome == "created":
            audit.record(db, "system.admin.bootstrapped",
                         f"Administrator account '{settings.admin_username}' created on first start",
                         target_type="user", target_label=settings.admin_username)
        elif outcome == "env-password":
            audit.record(db, "system.admin.password.env",
                         "Administrator password applied from SHIFTKEY_ADMIN_PASSWORD (account never used)",
                         target_type="user", target_label=settings.admin_username)
        audit.record(db, "system.startup", f"ShiftKey {__version__} started ({engine.url.get_backend_name()})",
                     meta={"version": __version__, "database": engine.url.get_backend_name()})
    sync_jobs.start_background()
    yield
    sync_jobs.stop_background()


app = FastAPI(title="ShiftKey", version=__version__, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Tag every request with an id and record refused API calls in the audit trail."""
    request.state.request_id = secrets.token_hex(8)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    path = request.url.path
    if path.startswith("/api/") and response.status_code in (401, 403, 429) \
            and not (response.status_code == 401 and path in ("/api/me", "/api/auth/login")):
        try:
            with SessionLocal() as db:
                actor = _load_user(db, request)
                reason = {401: "not signed in", 403: "forbidden", 429: "rate limited"}[response.status_code]
                who = actor.username if actor else "anonymous"
                audit.record(db, "access.denied", f"{who}: {request.method} {path} refused ({reason})",
                             request=request, actor=actor, outcome="denied",
                             meta={"method": request.method, "path": path, "status": response.status_code,
                                   "query": str(request.url.query)[:200]})
        except Exception:  # pragma: no cover - never let auditing break a response
            logging.getLogger("shiftkey.audit").exception("could not record denied access")
    return response


def _attachment(filename: str) -> dict:
    ascii_name = filename.encode("ascii", "ignore").decode() or "report"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"}


def active_user(user: User = Depends(current_user)) -> User:
    password_ok_for(user)
    return user


def _scoped_report(db: Session, report_id: int, user: User):
    report = get_report(db, report_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    scope = Scope(user)
    if not scope.unrestricted and not any(scope.allows(r) for r in report.records):
        raise HTTPException(404, "Report not found.")
    return report, scope


def _user_snapshot(u: User) -> dict:
    return {"username": u.username, "full_name": u.full_name, "role": u.role,
            "segment": u.segment if u.role == ROLE_HR else "all",
            "departments": u.dept_codes if u.role == ROLE_HOD else [],
            "is_active": u.is_active, "must_change_password": u.must_change_password}


# ----------------------------------------------------------------------------
# Meta / auth
# ----------------------------------------------------------------------------

@app.get("/api/health")
def health(db: Session = Depends(get_session)):
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # pragma: no cover
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "version": __version__}


@app.get("/api/config")
def config():
    return {"version": __version__, "database": engine.url.get_backend_name(),
            "segments": SEGMENT_LABELS, "segment_prefixes": settings.segment_map()}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_session)):
    username = body.username.strip().lower()
    if is_locked(request, username):
        audit.record(db, "auth.login.locked", f"Sign-in for '{username}' refused: too many failed attempts",
                     request=request, actor_username=username, outcome="denied")
        check_throttle(request, username)
    user = db.scalar(select(User).options(selectinload(User.departments)).where(User.username == username))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        note_failure(request, username)
        why = "unknown user" if user is None else ("account disabled" if not user.is_active else "wrong password")
        audit.record(db, "auth.login.failed", f"Sign-in failed for '{username}' ({why})",
                     request=request, actor_username=username, outcome="failure", meta={"reason": why},
                     target_type="user", target_id=user.id if user else None, target_label=username)
        raise HTTPException(401, "Invalid username or password.")
    clear_failures(request, username)
    create_session(db, user, request, response)
    audit.record(db, "auth.login.success", f"{user.full_name or user.username} signed in",
                 request=request, actor=user, target_type="user", target_id=user.id, target_label=user.username)
    return me_dict(user)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_session)):
    actor = _load_user(db, request)
    destroy_session(db, request, response)
    if actor:
        audit.record(db, "auth.logout", f"{actor.full_name or actor.username} signed out", request=request, actor=actor)
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return me_dict(user)


@app.post("/api/me/password")
def change_password(body: PasswordChangeIn, request: Request, response: Response,
                    user: User = Depends(current_user), db: Session = Depends(get_session)):
    if not verify_password(body.current_password, user.password_hash):
        audit.record(db, "auth.password.changed", f"{user.username}: password change refused (current password wrong)",
                     request=request, actor=user, outcome="failure")
        raise HTTPException(400, "Current password is incorrect.")
    if (err := validate_password(body.new_password)):
        raise HTTPException(400, err)
    if body.new_password == body.current_password:
        raise HTTPException(400, "New password must differ from the current one.")
    forced = user.must_change_password
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    revoke_user_sessions(db, user)
    db.commit()
    create_session(db, user, request, response)
    audit.record(db, "auth.password.changed",
                 f"{user.full_name or user.username} changed their password" + (" (first sign-in)" if forced else ""),
                 request=request, actor=user, target_type="user", target_id=user.id, target_label=user.username,
                 meta={"first_login": forced, "other_sessions_revoked": True})
    return me_dict(user)


# ----------------------------------------------------------------------------
# Users (admin)
# ----------------------------------------------------------------------------

def _validate_role_fields(role: str, segment: str, departments: list) -> None:
    if role not in ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(ROLES)}.")
    if role == ROLE_HR and segment not in ("indian", "local", "all"):
        raise HTTPException(400, "HR segment must be indian, local or all.")
    if role == ROLE_HOD and not departments:
        raise HTTPException(400, "Assign at least one department to a HOD.")


@app.get("/api/users")
def api_list_users(_: User = Depends(require_admin), db: Session = Depends(get_session)):
    users = db.scalars(select(User).options(selectinload(User.departments)).order_by(User.role, User.username)).all()
    return [user_dict(u) for u in users]


@app.post("/api/users", status_code=201)
def api_create_user(body: UserCreateIn, request: Request, admin: User = Depends(require_admin),
                    db: Session = Depends(get_session)):
    username = body.username.strip().lower()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "That username is taken.")
    _validate_role_fields(body.role, body.segment, body.departments)
    if (err := validate_password(body.password)):
        raise HTTPException(400, err)
    u = User(username=username, full_name=body.full_name.strip(), role=body.role,
             segment=body.segment if body.role == ROLE_HR else "all",
             password_hash=hash_password(body.password), must_change_password=body.must_change_password)
    if body.role == ROLE_HOD:
        for code in sorted(set(body.departments)):
            u.departments.append(UserDepartment(dept_code=code))
    db.add(u)
    db.commit()
    db.refresh(u)
    after = _user_snapshot(u)
    scope_txt = (f"departments {', '.join(after['departments'])}" if u.role == ROLE_HOD
                 else f"{SEGMENT_LABELS.get(u.segment, u.segment)} staff" if u.role == ROLE_HR else "full access")
    audit.record(db, "user.created", f"{admin.username} created {u.role.upper()} account '{u.username}' ({scope_txt})",
                 request=request, actor=admin, target_type="user", target_id=u.id, target_label=u.username, after=after)
    return user_dict(u)


@app.patch("/api/users/{user_id}")
def api_update_user(user_id: int, body: UserUpdateIn, request: Request, admin: User = Depends(require_admin),
                    db: Session = Depends(get_session)):
    u = db.scalar(select(User).options(selectinload(User.departments), selectinload(User.sessions)).where(User.id == user_id))
    if u is None:
        raise HTTPException(404, "User not found.")
    before = _user_snapshot(u)
    role = body.role or u.role
    segment = body.segment if body.segment is not None else u.segment
    depts = body.departments if body.departments is not None else u.dept_codes
    _validate_role_fields(role, segment, depts)
    if u.id == admin.id and (role != ROLE_ADMIN or body.is_active is False):
        raise HTTPException(400, "You cannot demote or deactivate your own account.")
    if body.full_name is not None:
        u.full_name = body.full_name.strip()
    u.role = role
    u.segment = segment if role == ROLE_HR else "all"
    if role == ROLE_HOD:
        wanted = set(depts)
        u.departments[:] = [d for d in u.departments if d.dept_code in wanted]
        have = {d.dept_code for d in u.departments}
        for code in sorted(wanted - have):
            u.departments.append(UserDepartment(dept_code=code))
    else:
        u.departments[:] = []
    status_event = None
    if body.is_active is not None and body.is_active != u.is_active:
        u.is_active = body.is_active
        status_event = "user.enabled" if u.is_active else "user.disabled"
        if not u.is_active:
            revoke_user_sessions(db, u)
    pw_reset = False
    if body.password:
        if (err := validate_password(body.password)):
            raise HTTPException(400, err)
        u.password_hash = hash_password(body.password)
        u.must_change_password = True if body.must_change_password is None else body.must_change_password
        revoke_user_sessions(db, u)
        pw_reset = True
    elif body.must_change_password is not None:
        u.must_change_password = body.must_change_password
    db.commit()
    db.refresh(u)
    after = _user_snapshot(u)
    changes = audit.diff(before, after)
    if changes:
        audit.record(db, "user.updated", f"{admin.username} updated '{u.username}': " +
                     ", ".join(f"{c['field']} {c['from']} → {c['to']}" for c in changes if c['field'] != 'must_change_password')[:300]
                     or "settings", request=request, actor=admin, target_type="user", target_id=u.id,
                     target_label=u.username, before=before, after=after)
    if status_event:
        audit.record(db, status_event, f"{admin.username} {'enabled' if u.is_active else 'disabled'} '{u.username}'",
                     request=request, actor=admin, target_type="user", target_id=u.id, target_label=u.username)
    if pw_reset:
        audit.record(db, "user.password.reset", f"{admin.username} reset the password of '{u.username}' (sessions revoked)",
                     request=request, actor=admin, target_type="user", target_id=u.id, target_label=u.username,
                     meta={"must_change_password": u.must_change_password})
    return user_dict(u)


@app.delete("/api/users/{user_id}", status_code=204)
def api_delete_user(user_id: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    u = db.scalar(select(User).options(selectinload(User.departments)).where(User.id == user_id))
    if u is None:
        raise HTTPException(404, "User not found.")
    if u.id == admin.id:
        raise HTTPException(400, "You cannot delete your own account.")
    before = _user_snapshot(u)
    db.delete(u)
    db.commit()
    audit.record(db, "user.deleted", f"{admin.username} deleted {before['role'].upper()} account '{before['username']}'",
                 request=request, actor=admin, target_type="user", target_id=user_id, target_label=before["username"], before=before)
    return Response(status_code=204)


# ----------------------------------------------------------------------------
# Sessions (admin)
# ----------------------------------------------------------------------------

@app.get("/api/sessions")
def api_sessions(_: User = Depends(require_admin), db: Session = Depends(get_session)):
    return list_sessions(db)


@app.delete("/api/sessions/{session_id}", status_code=204)
def api_revoke_session(session_id: int, request: Request, admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)):
    s = db.get(UserSession, session_id)
    if s is None:
        raise HTTPException(404, "Session not found.")
    u = db.get(User, s.user_id)
    db.delete(s)
    db.commit()
    audit.record(db, "auth.session.revoked", f"{admin.username} revoked a session of '{u.username if u else '?'}' from {s.ip or '?'}",
                 request=request, actor=admin, target_type="user", target_id=s.user_id,
                 target_label=u.username if u else None, meta={"session_ip": s.ip, "user_agent": s.user_agent})
    return Response(status_code=204)


# ----------------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------------

@app.get("/api/departments")
def api_departments(_: User = Depends(current_user), db: Session = Depends(get_session)):
    return list_departments(db)


@app.get("/api/shifts")
def api_shifts(_: User = Depends(current_user), db: Session = Depends(get_session)):
    return list_shifts(db)


@app.put("/api/shifts/{code}")
def api_put_shift(code: str, body: ShiftIn, request: Request, admin: User = Depends(require_admin),
                  db: Session = Depends(get_session)):
    code = code.strip()
    if not code:
        raise HTTPException(400, "Shift code required.")
    s = db.get(Shift, code)
    created = s is None
    before = None if created else {"name": s.name, "start": s.start, "end": s.end}
    if created:
        s = Shift(code=code)
        db.add(s)
    if body.name is not None:
        s.name = body.name.strip() or None
    if body.start is not None:
        s.start = body.start
    if body.end is not None:
        s.end = body.end
    s.source = "admin"
    s.updated_at = utcnow()
    db.commit()
    db.refresh(s)
    after = {"name": s.name, "start": s.start, "end": s.end}
    if created:
        audit.record(db, "shift.created", f"{admin.username} added shift {code} ({s.start or '?'}–{s.end or '?'})",
                     request=request, actor=admin, target_type="shift", target_id=code, target_label=code, after=after)
    elif before != after:
        audit.record(db, "shift.updated", f"{admin.username} changed shift {code}: " +
                     ", ".join(f"{c['field']} {c['from']} → {c['to']}" for c in audit.diff(before, after)),
                     request=request, actor=admin, target_type="shift", target_id=code, target_label=code,
                     before=before, after=after)
    return shift_dict(s)


@app.delete("/api/shifts/{code}", status_code=204)
def api_delete_shift(code: str, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    s = db.get(Shift, code)
    if s is None:
        raise HTTPException(404, "Shift not found.")
    before = {"name": s.name, "start": s.start, "end": s.end}
    db.delete(s)
    db.commit()
    audit.record(db, "shift.deleted", f"{admin.username} deleted shift {code}", request=request, actor=admin,
                 target_type="shift", target_id=code, target_label=code, before=before)
    return Response(status_code=204)


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

@app.get("/api/reports")
def api_list_reports(user: User = Depends(active_user), db: Session = Depends(get_session)):
    return list_reports(db, Scope(user))


@app.post("/api/reports/upload", status_code=201)
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    report_date: Optional[date] = Form(None),
    replace: bool = Form(True),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB.")
    try:
        parsed = parse_report(data, file.filename, report_date)
    except ParseError as exc:
        audit.record(db, "report.uploaded", f"{user.username}: upload of '{file.filename}' rejected – {exc}",
                     request=request, actor=user, outcome="failure", target_type="file", target_label=file.filename,
                     meta={"size": len(data), "error": str(exc)})
        raise HTTPException(422, str(exc)) from exc
    if parsed.report_date is None:
        raise HTTPException(422, "Report date could not be detected. Pick the date and upload again.")
    try:
        report, replaced, previous = save_report(db, parsed, data, replace=replace)
    except FileExistsError:
        raise HTTPException(409, f"A report for {parsed.report_date.isoformat()} already exists.")
    s = parsed.summary()
    after = {"source_filename": file.filename, "source_sha256": report.source_sha256, "total": s["total"],
             "present": s["present"], "absent": s["absent"], "halfday": s["halfday"], "mispunch": s["mispunch"],
             "warnings": s["warnings"], "departments": len(parsed.departments), "columns": parsed.columns}
    label = report.report_date.strftime("%d %b %Y")
    if replaced:
        lost = previous.get("corrections", 0) if previous else 0
        audit.record(db, "report.replaced",
                     f"{user.username} replaced the report for {label} with '{file.filename}' ({s['total']} employees"
                     + (f", {lost} correction(s) discarded" if lost else "") + ")",
                     request=request, actor=user, target_type="report", target_id=report.id, target_label=label,
                     report_id=report.id, report_date=report.report_date, before=previous, after=after,
                     meta={"size": len(data), "corrections_lost": lost}, severity="critical" if lost else None)
    else:
        audit.record(db, "report.uploaded",
                     f"{user.username} imported the report for {label} from '{file.filename}' "
                     f"({s['total']} employees, {s['mispunch']} mispunch, {s['halfday']} half day)",
                     request=request, actor=user, target_type="report", target_id=report.id, target_label=label,
                     report_id=report.id, report_date=report.report_date, after=after, meta={"size": len(data)})
    sync_jobs.trigger_auto_push([report.id])
    return report_detail(db, report, Scope(user), replaced=replaced)


@app.post("/api/parse")
async def api_parse_only(request: Request, file: UploadFile = File(...), report_date: Optional[date] = Form(None),
                         user: User = Depends(require_admin), db: Session = Depends(get_session)):
    data = await file.read()
    try:
        parsed = parse_report(data, file.filename, report_date)
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(db, "report.parsed", f"{user.username} parsed '{file.filename}' without storing it",
                 request=request, actor=user, target_type="file", target_label=file.filename,
                 report_date=parsed.report_date, meta={"total": parsed.total})
    return {
        "report_date": parsed.report_date.isoformat() if parsed.report_date else None,
        "company": parsed.company, "title": parsed.title, "run_at": parsed.run_at,
        "columns": parsed.columns, "problems": parsed.problems,
        "summary": parsed.summary(), "records": [r.to_dict() for r in parsed.records],
    }


@app.get("/api/reports/{report_id}")
def api_get_report(report_id: int, request: Request, user: User = Depends(active_user), db: Session = Depends(get_session)):
    report, scope = _scoped_report(db, report_id, user)
    detail = report_detail(db, report, scope)
    audit.record(db, "report.viewed", f"{user.username} viewed the report for {report.report_date.strftime('%d %b %Y')}"
                 f" ({detail['summary']['total']} employees in scope)",
                 request=request, actor=user, target_type="report", target_id=report.id,
                 target_label=report.report_date.strftime("%d %b %Y"), report_id=report.id, report_date=report.report_date)
    return detail


@app.delete("/api/reports/{report_id}", status_code=204)
def api_delete_report(report_id: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    report = get_report(db, report_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    before = report_snapshot(report)
    label = report.report_date.strftime("%d %b %Y")
    rdate = report.report_date
    delete_report(db, report)
    audit.record(db, "report.deleted", f"{admin.username} deleted the report for {label} "
                 f"({before['total']} employees, {before['corrections']} correction(s) lost)",
                 request=request, actor=admin, target_type="report", target_id=report_id, target_label=label,
                 report_id=report_id, report_date=rdate, before=before)
    return Response(status_code=204)


@app.get("/api/reports/{report_id}/export")
def api_export(
    report_id: int,
    request: Request,
    format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
    highlight: bool = True,
    summary: bool = True,
    only: Optional[str] = Query(None, pattern="^(flagged|pending|halfday|mispunch|absent|corrected)$"),
    segment: Optional[str] = Query(None, pattern="^(indian|local|other)$"),
    dept: Optional[str] = None,
    user: User = Depends(active_user),
    db: Session = Depends(get_session),
):
    report, scope = _scoped_report(db, report_id, user)
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    records = [record_dict(r, scope, shifts) for r in visible_records(report, scope)]
    if segment:
        records = [r for r in records if r["segment"] == segment]
    if dept:
        records = [r for r in records if r["dept_code"] == dept]
    filters = {
        "flagged": lambda r: r["flag_mispunch"] or r["flag_warning"] or r["is_halfday"],
        "pending": lambda r: r["needs_action"],
        "halfday": lambda r: r["is_halfday"],
        "mispunch": lambda r: r["flag_mispunch"],
        "absent": lambda r: r["is_absent"],
        "corrected": lambda r: r["is_corrected"],
    }
    if only:
        records = [r for r in records if filters[only](r)]

    parts = [settings.report_name_suffix]
    seg = segment or scope.segment
    if seg:
        parts.append(SEGMENT_LABELS.get(seg, seg))
    if dept:
        parts.append(dept)
    if only:
        parts.append(only)
    suffix = " ".join(parts)
    filename = export_filename(report.report_date, suffix, format)
    if format == "csv":
        body = build_csv(report_date=report.report_date, records=records)
        media = "text/csv; charset=utf-8"
    else:
        wb = build_workbook(report_date=report.report_date, company=report.company, title=report.title,
                            records=records, highlight=highlight, summary=summary, run_at=report.run_at)
        body = workbook_bytes(wb)
        media = XLSX_MIME
    label = report.report_date.strftime("%d %b %Y")
    audit.record(db, "report.exported", f"{user.username} exported {label} as {format.upper()} ({len(records)} employees)"
                 + (f", filter {only}" if only else "") + (f", {SEGMENT_LABELS.get(seg, seg)} staff" if seg else "")
                 + (f", department {dept}" if dept else ""),
                 request=request, actor=user, target_type="report", target_id=report.id, target_label=label,
                 report_id=report.id, report_date=report.report_date, dept_code=dept, segment=seg,
                 meta={"format": format, "filename": filename, "rows": len(records), "only": only,
                       "segment": seg, "dept": dept, "highlight": highlight, "summary_sheets": summary,
                       "sha256": audit.hashlib.sha256(body).hexdigest()})
    return Response(body, media_type=media, headers=_attachment(filename))


@app.get("/api/reports/{report_id}/source")
def api_source(report_id: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    report = get_report(db, report_id)
    if report is None or not report.source_bytes:
        raise HTTPException(404, "Original file not available.")
    name = report.source_filename or f"{report.report_date.isoformat()}-source.xlsx"
    audit.record(db, "report.source.downloaded", f"{admin.username} downloaded the original file '{name}' "
                 f"for {report.report_date.strftime('%d %b %Y')}", request=request, actor=admin,
                 target_type="report", target_id=report.id, target_label=report.report_date.strftime("%d %b %Y"),
                 report_id=report.id, report_date=report.report_date,
                 meta={"filename": name, "sha256": report.source_sha256, "size": len(report.source_bytes)})
    return Response(report.source_bytes, media_type="application/octet-stream", headers=_attachment(name))


@app.get("/api/reports/{report_id}/history")
def api_report_history(report_id: int, user: User = Depends(active_user), db: Session = Depends(get_session)):
    _scoped_report(db, report_id, user)
    stmt = audit.scope_filter(user, select(AuditEvent).where(AuditEvent.report_id == report_id)
                              .order_by(AuditEvent.id.desc()).limit(500))
    return [audit.event_dict(e) for e in db.scalars(stmt).all()]


# ----------------------------------------------------------------------------
# Corrections
# ----------------------------------------------------------------------------

def _correctable(db: Session, record_id: int, user: User) -> AttendanceRecord:
    rec = db.scalar(select(AttendanceRecord).options(selectinload(AttendanceRecord.corrections), selectinload(AttendanceRecord.report))
                    .where(AttendanceRecord.id == record_id))
    if rec is None:
        raise HTTPException(404, "Record not found.")
    scope = Scope(user)
    if not scope.allows(rec):
        raise HTTPException(404, "Record not found.")
    if not scope.can_correct(rec):
        raise HTTPException(403, "You can only resolve punches for your own department(s).")
    return rec


ACTION_WORDS = {"shift_time": "using the shift time", "set_time": "with the actual time",
                "halfday": "as a half day", "absent": "as absent"}


@app.post("/api/records/{record_id}/correction")
def api_correct(record_id: int, body: CorrectionIn, request: Request, user: User = Depends(require_corrector),
                db: Session = Depends(get_session)):
    password_ok_for(user)
    rec = _correctable(db, record_id, user)
    before = record_snapshot(rec)
    try:
        c = apply_correction(db, rec, user, body.action, body.field, body.time, body.remark)
    except ValueError as exc:
        audit.record(db, "correction.applied", f"{user.username}: correction of {rec.name} refused – {exc}",
                     request=request, actor=user, outcome="failure", target_type="record", target_id=rec.id,
                     target_label=rec.name, report_id=rec.report_id, report_date=rec.report.report_date,
                     dept_code=rec.dept_code, segment=rec.segment, paycode=rec.paycode,
                     meta={"action": body.action, "field": body.field, "time": body.time, "error": str(exc)})
        raise HTTPException(400, str(exc))
    after = record_snapshot(rec)
    label = rec.report.report_date.strftime("%d %b %Y")
    what = (f"{(c.field or 'out').upper()} {before['time_out' if c.field != 'in' else 'time_in'] or '—'} → "
            f"{after['time_out' if c.field != 'in' else 'time_in']}" if c.action in ("shift_time", "set_time")
            else f"status {before['status']} → {after['status']}")
    audit.record(db, "correction.applied",
                 f"{user.full_name or user.username} resolved {rec.name} ({rec.dept_name}, {label}) "
                 f"{ACTION_WORDS.get(c.action, c.action)}: {what}" + (f" – \"{c.remark}\"" if c.remark else ""),
                 request=request, actor=user, target_type="record", target_id=rec.id, target_label=rec.name,
                 report_id=rec.report_id, report_date=rec.report.report_date, dept_code=rec.dept_code,
                 segment=rec.segment, paycode=rec.paycode, before=before, after=after,
                 meta={"correction_id": c.id, "action": c.action, "field": c.field, "remark": c.remark, "shift": rec.shift})
    sync_jobs.trigger_auto_push([rec.report_id])
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    return record_dict(rec, Scope(user), shifts)


@app.delete("/api/records/{record_id}/correction")
def api_revert(record_id: int, request: Request, remark: Optional[str] = None,
               user: User = Depends(require_corrector), db: Session = Depends(get_session)):
    password_ok_for(user)
    rec = _correctable(db, record_id, user)
    before = record_snapshot(rec)
    was = rec.is_corrected
    revert_correction(db, rec, user, remark)
    if was:
        after = record_snapshot(rec)
        audit.record(db, "correction.reverted",
                     f"{user.full_name or user.username} reverted {rec.name} ({rec.dept_name}, "
                     f"{rec.report.report_date.strftime('%d %b %Y')}) to the machine values "
                     f"{after['time_in'] or '—'} → {after['time_out'] or '—'} {after['status']}" + (f" – \"{remark}\"" if remark else ""),
                     request=request, actor=user, target_type="record", target_id=rec.id, target_label=rec.name,
                     report_id=rec.report_id, report_date=rec.report.report_date, dept_code=rec.dept_code,
                     segment=rec.segment, paycode=rec.paycode, before=before, after=after, meta={"remark": remark})
        sync_jobs.trigger_auto_push([rec.report_id])
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    return record_dict(rec, Scope(user), shifts)


@app.get("/api/corrections")
def api_corrections(report_id: Optional[int] = None, limit: int = Query(300, le=2000),
                    user: User = Depends(active_user), db: Session = Depends(get_session)):
    return list_corrections(db, Scope(user), report_id, limit)


@app.get("/api/records/{record_id}/history")
def api_record_history(record_id: int, user: User = Depends(active_user), db: Session = Depends(get_session)):
    """Everything that ever happened to one attendance row: import, corrections, reverts, exports."""
    rec = db.scalar(select(AttendanceRecord).options(selectinload(AttendanceRecord.corrections), selectinload(AttendanceRecord.report))
                    .where(AttendanceRecord.id == record_id))
    if rec is None or not Scope(user).allows(rec):
        raise HTTPException(404, "Record not found.")
    report = rec.report
    events = db.scalars(select(AuditEvent).where(AuditEvent.target_type == "record", AuditEvent.target_id == str(rec.id),
                                                 AuditEvent.report_id == report.id).order_by(AuditEvent.id)).all()
    imported = db.scalar(select(AuditEvent).where(AuditEvent.report_id == report.id,
                                                  AuditEvent.action.in_(["report.uploaded", "report.replaced"]))
                         .order_by(AuditEvent.id.desc()))
    shifts = {s.code: s for s in db.scalars(select(Shift)).all()}
    return {
        "record": record_dict(rec, Scope(user), shifts),
        "report": {"id": report.id, "report_date": report.report_date.isoformat(), "source_filename": report.source_filename,
                   "source_sha256": report.source_sha256, "uploaded_at": report.uploaded_at.isoformat() + "Z"},
        "imported": audit.event_dict(imported) if imported else None,
        "corrections": [correction_dict(c, rec, report) for c in rec.corrections],
        "events": [audit.event_dict(e) for e in events],
        "other_days": [
            {"report_id": r.report_id, "report_date": r.report.report_date.isoformat(), "record_id": r.id,
             "time_in": r.corrected_in if r.is_corrected else r.time_in,
             "time_out": r.corrected_out if r.is_corrected else r.time_out,
             "status": (r.corrected_status if r.is_corrected else r.status or "").strip(), "corrected": r.is_corrected}
            for r in db.scalars(select(AttendanceRecord).options(selectinload(AttendanceRecord.report))
                                .join(Report).where(AttendanceRecord.paycode == rec.paycode)
                                .order_by(Report.report_date.desc()).limit(31)).all()
            if Scope(user).allows(r)
        ],
    }


# ----------------------------------------------------------------------------
# Audit trail
# ----------------------------------------------------------------------------

def _audit_query(user: User, period: Optional[str], since: Optional[datetime], until: Optional[datetime],
                 category, action, actor_id, outcome, q, report_id, target, include_views, severity):
    stmt = audit.scope_filter(user, select(AuditEvent))
    since = since or audit.period_start(period)
    return audit.apply_filters(stmt, since=since, until=until, category=category, action=action,
                               actor_id=actor_id, outcome=outcome, q=q, report_id=report_id, target=target,
                               include_views=include_views, severity=severity)


@app.get("/api/audit")
def api_audit(
    period: Optional[str] = Query("7d"), since: Optional[datetime] = None, until: Optional[datetime] = None,
    category: Optional[str] = None, action: Optional[str] = None, actor_id: Optional[int] = None,
    outcome: Optional[str] = None, q: Optional[str] = None, report_id: Optional[int] = None,
    target: Optional[str] = None, include_views: bool = False, severity: Optional[str] = None,
    before_id: Optional[int] = None, limit: int = Query(100, ge=1, le=500),
    user: User = Depends(active_user), db: Session = Depends(get_session),
):
    stmt = _audit_query(user, period, since, until, category, action, actor_id, outcome, q, report_id, target, include_views, severity)
    if before_id:
        stmt = stmt.where(AuditEvent.id < before_id)
    rows = db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"events": [audit.event_dict(e) for e in rows], "has_more": more,
            "next_before_id": rows[-1].id if rows and more else None}


@app.get("/api/audit/stats")
def api_audit_stats(period: Optional[str] = Query("7d"), user: User = Depends(active_user), db: Session = Depends(get_session)):
    since = audit.period_start(period) or datetime(1970, 1, 1)
    out = audit.stats(db, user, since)
    out["period"] = period
    out["total_events"] = int(db.execute(select(audit.func.count()).select_from(AuditEvent)).scalar() or 0)
    last = db.scalar(select(AuditEvent).where(AuditEvent.action == "audit.verified").order_by(AuditEvent.id.desc()))
    out["last_verified"] = audit.event_dict(last) if last else None
    return out


@app.get("/api/audit/actions")
def api_audit_actions(_: User = Depends(active_user)):
    return {"actions": [{"action": a, "category": c, "severity": s, "label": l} for a, (c, s, l) in audit.CATALOG.items()],
            "categories": [{"key": k, "label": audit.CATEGORY_LABELS[k]} for k in audit.CATEGORIES]}


@app.get("/api/audit/actors")
def api_audit_actors(user: User = Depends(active_user), db: Session = Depends(get_session)):
    stmt = audit.scope_filter(user, select(AuditEvent.actor_id, AuditEvent.actor_username, AuditEvent.actor_name)
                              .where(AuditEvent.actor_id.isnot(None)).distinct())
    return [{"id": i, "username": u, "name": n} for i, u, n in db.execute(stmt).all()]


@app.post("/api/audit/verify")
def api_audit_verify(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_session)):
    result = audit.verify_chain(db)
    audit.record(db, "audit.verified",
                 f"{admin.username} verified the audit chain: " +
                 (f"intact, {result['checked']} events" if result["ok"] else f"BROKEN at event #{result['broken_at']} – {result['reason']}"),
                 request=request, actor=admin, outcome="success" if result["ok"] else "failure",
                 severity="info" if result["ok"] else "critical", meta=result)
    return result


@app.get("/api/audit/export")
def api_audit_export(
    request: Request, format: str = Query("csv", pattern="^(csv|json)$"),
    period: Optional[str] = Query("all"), since: Optional[datetime] = None, until: Optional[datetime] = None,
    category: Optional[str] = None, action: Optional[str] = None, actor_id: Optional[int] = None,
    outcome: Optional[str] = None, q: Optional[str] = None, report_id: Optional[int] = None,
    target: Optional[str] = None, include_views: bool = True, severity: Optional[str] = None,
    admin: User = Depends(require_admin), db: Session = Depends(get_session),
):
    stmt = _audit_query(admin, period, since, until, category, action, actor_id, outcome, q, report_id, target, include_views, severity)
    events = [audit.event_dict(e) for e in db.scalars(stmt.order_by(AuditEvent.id)).all()]
    stamp = utcnow().strftime("%Y-%m-%d %H%M")
    filename = f"ShiftKey audit {stamp}.{format}"
    if format == "csv":
        body, media = audit.to_csv(events), "text/csv; charset=utf-8"
    else:
        body = json.dumps({"exported_at": utcnow().isoformat() + "Z", "exported_by": admin.username,
                           "count": len(events), "events": events}, ensure_ascii=False, indent=1).encode("utf-8")
        media = "application/json"
    audit.record(db, "audit.exported", f"{admin.username} exported {len(events)} audit events as {format.upper()}",
                 request=request, actor=admin, meta={"format": format, "count": len(events), "period": period,
                                                     "filters": {"category": category, "action": action, "actor_id": actor_id,
                                                                 "outcome": outcome, "q": q, "report_id": report_id},
                                                     "sha256": audit.hashlib.sha256(body).hexdigest()})
    return Response(body, media_type=media, headers=_attachment(filename))


app.include_router(sync_router)


# ----------------------------------------------------------------------------
# Static UI (must be mounted last)
# ----------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
