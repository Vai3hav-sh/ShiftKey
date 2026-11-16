"""Append-only, hash-chained audit trail.

Every security- or data-relevant action calls :func:`record`. Events are never
updated or deleted through the application; each one stores the SHA-256 of the
previous event plus its own canonical content, so any later edit or deletion in
the database breaks the chain and :func:`verify_chain` reports exactly where.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import ROLE_ADMIN, ROLE_HOD, ROLE_HR, AuditEvent, User, utcnow

# ----------------------------------------------------------------------------
# Catalogue: action -> (category, severity, human label)
# ----------------------------------------------------------------------------

CATALOG: Dict[str, Tuple[str, str, str]] = {
    "auth.login.success": ("auth", "info", "Signed in"),
    "auth.login.failed": ("auth", "warning", "Sign-in failed"),
    "auth.login.locked": ("auth", "warning", "Sign-in locked out"),
    "auth.logout": ("auth", "info", "Signed out"),
    "auth.password.changed": ("auth", "notice", "Password changed"),
    "auth.session.revoked": ("auth", "notice", "Session revoked"),
    "user.created": ("user", "notice", "User created"),
    "user.updated": ("user", "notice", "User updated"),
    "user.deleted": ("user", "warning", "User deleted"),
    "user.password.reset": ("user", "warning", "Password reset by admin"),
    "user.disabled": ("user", "warning", "User disabled"),
    "user.enabled": ("user", "notice", "User enabled"),
    "report.uploaded": ("report", "notice", "Report imported"),
    "report.replaced": ("report", "warning", "Report replaced"),
    "report.deleted": ("report", "critical", "Report deleted"),
    "report.viewed": ("report", "info", "Report viewed"),
    "report.parsed": ("report", "info", "File parsed (not stored)"),
    "report.exported": ("export", "notice", "Report exported"),
    "report.source.downloaded": ("export", "notice", "Original file downloaded"),
    "correction.applied": ("correction", "notice", "Punch corrected"),
    "correction.reverted": ("correction", "warning", "Correction reverted"),
    "shift.created": ("shift", "notice", "Shift added"),
    "shift.updated": ("shift", "notice", "Shift changed"),
    "shift.deleted": ("shift", "warning", "Shift deleted"),
    "access.denied": ("access", "warning", "Access denied"),
    "connection.created": ("integration", "notice", "Connection added"),
    "connection.updated": ("integration", "notice", "Connection changed"),
    "connection.deleted": ("integration", "warning", "Connection deleted"),
    "connection.tested": ("integration", "info", "Connection tested"),
    "sql.query": ("integration", "notice", "SQL query run"),
    "sql.schema": ("integration", "info", "Schema browsed"),
    "sync.job.created": ("integration", "notice", "Sync job created"),
    "sync.job.updated": ("integration", "notice", "Sync job changed"),
    "sync.job.deleted": ("integration", "warning", "Sync job deleted"),
    "sync.pull": ("integration", "notice", "Data pulled from SQL"),
    "sync.push": ("integration", "notice", "Data pushed to SQL"),
    "audit.exported": ("audit", "notice", "Audit log exported"),
    "audit.verified": ("audit", "info", "Integrity check run"),
    "system.startup": ("system", "info", "Application started"),
    "system.migrated": ("system", "notice", "Database migrated"),
    "system.admin.bootstrapped": ("system", "critical", "Admin account created"),
    "system.admin.password.env": ("system", "warning", "Admin password set from environment"),
}

CATEGORIES = ["auth", "user", "report", "correction", "export", "shift", "integration", "access", "audit", "system"]
CATEGORY_LABELS = {
    "auth": "Sign-ins", "user": "Users", "report": "Reports", "correction": "Corrections",
    "export": "Exports", "shift": "Shifts", "integration": "Integrations", "access": "Denied",
    "audit": "Audit", "system": "System",
}

_chain_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Hashing
# ----------------------------------------------------------------------------

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def hash_payload(prev_hash: Optional[str], payload: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(b"\n")
    h.update(_canonical(payload).encode("utf-8"))
    return h.hexdigest()


def event_payload(e: AuditEvent) -> Dict[str, Any]:
    return {
        "ts": e.ts.isoformat() if e.ts else None, "request_id": e.request_id,
        "actor_id": e.actor_id, "actor_username": e.actor_username, "actor_role": e.actor_role,
        "ip": e.ip, "action": e.action, "category": e.category, "severity": e.severity,
        "outcome": e.outcome, "summary": e.summary, "target_type": e.target_type,
        "target_id": e.target_id, "target_label": e.target_label, "report_id": e.report_id,
        "report_date": e.report_date.isoformat() if e.report_date else None,
        "dept_code": e.dept_code, "segment": e.segment, "paycode": e.paycode,
        "before": e.before, "after": e.after, "changes": e.changes, "meta": e.meta,
    }


# ----------------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------------

def diff(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
         ignore: Iterable[str] = ("password_hash",)) -> List[Dict[str, Any]]:
    if not before and not after:
        return []
    before, after = before or {}, after or {}
    keys = [k for k in dict.fromkeys(list(before) + list(after)) if k not in ignore]
    out = []
    for k in keys:
        b, a = before.get(k), after.get(k)
        if b != a:
            out.append({"field": k, "from": b, "to": a})
    return out


def request_context(request: Optional[Request]) -> Dict[str, Optional[str]]:
    if request is None:
        return {"ip": None, "user_agent": None, "request_id": None}
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": (request.headers.get("user-agent") or "")[:200] or None,
        "request_id": getattr(request.state, "request_id", None),
    }


def record(db: Session, action: str, summary: str, *, request: Optional[Request] = None,
           actor: Optional[User] = None, actor_username: Optional[str] = None,
           outcome: str = "success", target_type: Optional[str] = None, target_id: Any = None,
           target_label: Optional[str] = None, report_id: Optional[int] = None,
           report_date: Optional[date] = None, dept_code: Optional[str] = None,
           segment: Optional[str] = None, paycode: Optional[str] = None,
           before: Optional[Dict[str, Any]] = None, after: Optional[Dict[str, Any]] = None,
           meta: Optional[Dict[str, Any]] = None, severity: Optional[str] = None) -> AuditEvent:
    category, default_sev, _ = CATALOG.get(action, ("system", "info", action))
    ctx = request_context(request)
    ev = AuditEvent(
        ts=utcnow(), request_id=ctx["request_id"],
        actor_id=actor.id if actor else None,
        actor_username=(actor.username if actor else actor_username),
        actor_name=(actor.full_name or actor.username) if actor else None,
        actor_role=actor.role if actor else None,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        action=action, category=category, severity=severity or default_sev, outcome=outcome,
        summary=summary[:400], target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        target_label=(target_label or "")[:200] or None,
        report_id=report_id, report_date=report_date, dept_code=dept_code, segment=segment,
        paycode=paycode, before=before, after=after,
        changes=diff(before, after) if (before is not None or after is not None) else None,
        meta=meta,
    )
    with _chain_lock:
        prev = db.execute(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)).scalar()
        ev.prev_hash = prev
        ev.hash = hash_payload(prev, event_payload(ev))
        db.add(ev)
        db.commit()
    return ev


# ----------------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------------

def verify_chain(db: Session) -> Dict[str, Any]:
    prev: Optional[str] = None
    checked = 0
    last_id = None
    q = select(AuditEvent).order_by(AuditEvent.id)
    for ev in db.execute(q).scalars().yield_per(500):
        checked += 1
        last_id = ev.id
        if ev.prev_hash != prev:
            return {"ok": False, "checked": checked, "broken_at": ev.id,
                    "reason": "previous-hash link does not match (an event was removed or inserted)"}
        expected = hash_payload(prev, event_payload(ev))
        if expected != ev.hash:
            return {"ok": False, "checked": checked, "broken_at": ev.id,
                    "reason": "event content does not match its hash (an event was modified)"}
        prev = ev.hash
    return {"ok": True, "checked": checked, "last_id": last_id, "head": prev}


# ----------------------------------------------------------------------------
# Querying
# ----------------------------------------------------------------------------

def scope_filter(user: User, stmt):
    """Admins see everything. Others see their own actions, report life-cycle events and
    the corrections that fall inside their department / segment scope."""
    if user.role == ROLE_ADMIN:
        return stmt
    own = or_((AuditEvent.actor_id == user.id) & (AuditEvent.actor_username == user.username),
              (AuditEvent.actor_id.is_(None)) & (AuditEvent.actor_username == user.username))  # own failed sign-ins
    lifecycle = AuditEvent.action.in_(["report.uploaded", "report.replaced", "report.deleted"])
    if user.role == ROLE_HOD:
        in_scope = (AuditEvent.category == "correction") & AuditEvent.dept_code.in_(user.dept_codes or ["__none__"])
    elif user.role == ROLE_HR and user.segment and user.segment != "all":
        in_scope = (AuditEvent.category == "correction") & (AuditEvent.segment == user.segment)
    else:
        in_scope = AuditEvent.category == "correction"
    return stmt.where(or_(own, lifecycle, in_scope))


def apply_filters(stmt, *, since: Optional[datetime] = None, until: Optional[datetime] = None,
                  category: Optional[str] = None, action: Optional[str] = None,
                  actor_id: Optional[int] = None, outcome: Optional[str] = None,
                  q: Optional[str] = None, report_id: Optional[int] = None,
                  target: Optional[str] = None, include_views: bool = True,
                  severity: Optional[str] = None):
    if since is not None:
        stmt = stmt.where(AuditEvent.ts >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.ts < until)
    if category:
        stmt = stmt.where(AuditEvent.category.in_(category.split(",")))
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if severity:
        stmt = stmt.where(AuditEvent.severity.in_(severity.split(",")))
    if report_id is not None:
        stmt = stmt.where(AuditEvent.report_id == report_id)
    if target:
        ttype, _, tid = target.partition(":")
        stmt = stmt.where(AuditEvent.target_type == ttype)
        if tid:
            stmt = stmt.where(AuditEvent.target_id == tid)
    if not include_views:
        stmt = stmt.where(AuditEvent.action != "report.viewed")
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            AuditEvent.summary.ilike(like), AuditEvent.target_label.ilike(like),
            AuditEvent.paycode.ilike(like), AuditEvent.actor_username.ilike(like),
            AuditEvent.actor_name.ilike(like), AuditEvent.ip.ilike(like),
            AuditEvent.request_id.ilike(like), AuditEvent.action.ilike(like),
        ))
    return stmt


def event_dict(e: AuditEvent) -> Dict[str, Any]:
    _, _, label = CATALOG.get(e.action, ("system", "info", e.action))
    return {
        "id": e.id, "ts": e.ts.isoformat() + "Z", "request_id": e.request_id,
        "actor": {"id": e.actor_id, "username": e.actor_username, "name": e.actor_name, "role": e.actor_role},
        "ip": e.ip, "user_agent": e.user_agent,
        "action": e.action, "action_label": label, "category": e.category,
        "category_label": CATEGORY_LABELS.get(e.category, e.category),
        "severity": e.severity, "outcome": e.outcome, "summary": e.summary,
        "target": {"type": e.target_type, "id": e.target_id, "label": e.target_label},
        "report_id": e.report_id, "report_date": e.report_date.isoformat() if e.report_date else None,
        "dept_code": e.dept_code, "segment": e.segment, "paycode": e.paycode,
        "before": e.before, "after": e.after, "changes": e.changes or [], "meta": e.meta or {},
        "hash": e.hash, "prev_hash": e.prev_hash,
    }


def stats(db: Session, user: User, since: datetime) -> Dict[str, Any]:
    base = select(AuditEvent).where(AuditEvent.ts >= since)
    base = scope_filter(user, base).subquery()
    def count(where=None):
        s = select(func.count()).select_from(base)
        if where is not None:
            s = s.where(where)
        return int(db.execute(s).scalar() or 0)
    return {
        "events": count(),
        "login_failures": count(base.c.action.in_(["auth.login.failed", "auth.login.locked"])),
        "denied": count(base.c.action == "access.denied"),
        "corrections": count(base.c.category == "correction"),
        "exports": count(base.c.category == "export"),
        "changes": count(base.c.category.in_(["user", "shift", "report"]) & (base.c.action != "report.viewed")),
        "actors": int(db.execute(select(func.count(func.distinct(base.c.actor_id)))).scalar() or 0),
    }


def to_csv(events: Sequence[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp_utc", "actor", "actor_role", "ip", "action", "category", "severity",
                "outcome", "summary", "target_type", "target_id", "target_label", "report_date",
                "department", "segment", "paycode", "changes", "meta", "request_id", "hash", "prev_hash"])
    for e in events:
        w.writerow([
            e["id"], e["ts"], e["actor"]["username"] or "", e["actor"]["role"] or "", e["ip"] or "",
            e["action"], e["category"], e["severity"], e["outcome"], e["summary"],
            e["target"]["type"] or "", e["target"]["id"] or "", e["target"]["label"] or "",
            e["report_date"] or "", e["dept_code"] or "", e["segment"] or "", e["paycode"] or "",
            "; ".join(f"{c['field']}: {c['from']} -> {c['to']}" for c in e["changes"]),
            json.dumps(e["meta"], ensure_ascii=False) if e["meta"] else "",
            e["request_id"] or "", e["hash"], e["prev_hash"] or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


def period_start(period: Optional[str]) -> Optional[datetime]:
    now = utcnow()
    if not period or period == "all":
        return None
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period.endswith("d") and period[:-1].isdigit():
        return now - timedelta(days=int(period[:-1]))
    if period.endswith("h") and period[:-1].isdigit():
        return now - timedelta(hours=int(period[:-1]))
    return None
