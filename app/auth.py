"""Password hashing, server-side sessions and FastAPI auth dependencies."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta
from typing import Dict, Optional, Tuple

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import get_session
from .models import ROLE_ADMIN, ROLE_HOD, ROLE_HR, User, UserSession, utcnow

log = logging.getLogger("shiftkey.auth")
COOKIE = "shiftkey_session"
CSRF_HEADER = "x-requested-with"

# ----------------------------------------------------------------------------
# Passwords (scrypt from the standard library – no extra dependency)
# ----------------------------------------------------------------------------

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                              n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(calc.hex(), dk)
    except (ValueError, TypeError):
        return False


def validate_password(pw: str) -> Optional[str]:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw.lower() == pw or pw.upper() == pw or not any(ch.isdigit() for ch in pw):
        return "Use upper and lower case letters and at least one digit."
    return None


# ----------------------------------------------------------------------------
# Login throttling (per process, good enough for a LAN deployment)
# ----------------------------------------------------------------------------

_failures: Dict[str, Tuple[int, float]] = {}


def _throttle_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{username.lower()}"


def is_locked(request: Request, username: str) -> bool:
    count, until = _failures.get(_throttle_key(request, username), (0, 0))
    return count >= settings.login_max_failures and time.time() < until


def check_throttle(request: Request, username: str) -> None:
    count, until = _failures.get(_throttle_key(request, username), (0, 0))
    if count >= settings.login_max_failures and time.time() < until:
        wait = int((until - time.time()) // 60) + 1
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait} min.")


def note_failure(request: Request, username: str) -> None:
    key = _throttle_key(request, username)
    count, _ = _failures.get(key, (0, 0))
    _failures[key] = (count + 1, time.time() + settings.login_lockout_minutes * 60)


def clear_failures(request: Request, username: str) -> None:
    _failures.pop(_throttle_key(request, username), None)


# ----------------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    sess = UserSession(
        token_hash=_token_hash(token), user_id=user.id,
        expires_at=utcnow() + timedelta(hours=settings.session_hours),
        user_agent=(request.headers.get("user-agent") or "")[:200],
        ip=request.client.host if request.client else None,
    )
    db.add(sess)
    user.last_login_at = utcnow()
    db.commit()
    response.set_cookie(
        COOKIE, token, max_age=settings.session_hours * 3600, httponly=True,
        samesite="lax", secure=settings.secure_cookies, path="/",
    )


def destroy_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
        if sess:
            db.delete(sess)
            db.commit()
    response.delete_cookie(COOKIE, path="/")


def revoke_user_sessions(db: Session, user: User) -> None:
    for s in list(user.sessions):
        db.delete(s)


def _load_user(db: Session, request: Request) -> Optional[User]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    sess = db.scalar(
        select(UserSession).options(selectinload(UserSession.user).selectinload(User.departments))
        .where(UserSession.token_hash == _token_hash(token))
    )
    if sess is None:
        return None
    now = utcnow()
    if sess.expires_at < now or not sess.user.is_active:
        db.delete(sess)
        db.commit()
        return None
    if (now - sess.last_seen_at).total_seconds() > 300:
        sess.last_seen_at = now
        db.commit()
    return sess.user


# ----------------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    user = _load_user(db, request)
    if user is None:
        raise HTTPException(401, "Please sign in.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER, "").lower() != "shiftkey":
        raise HTTPException(403, "Missing request header.")
    return user


def optional_user(request: Request, db: Session = Depends(get_session)) -> Optional[User]:
    return _load_user(db, request)


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "You do not have permission to do that.")
        return user
    return dep


require_admin = require_role(ROLE_ADMIN)
require_corrector = require_role(ROLE_ADMIN, ROLE_HOD)


def password_ok_for(user: User) -> None:
    """Block everything except password change while a reset is pending."""
    if user.must_change_password:
        raise HTTPException(428, "Please change your password before continuing.")


# ----------------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------------

def ensure_admin(db: Session) -> Optional[str]:
    """Create the first admin if needed. Returns 'created', 'env-password' or None."""
    admin = db.scalar(select(User).where(User.role == ROLE_ADMIN).order_by(User.id))
    if admin is not None:
        # The configured password keeps applying until someone has actually signed in as
        # admin, so setting SHIFTKEY_ADMIN_PASSWORD after the first start still works.
        if settings.admin_password and admin.last_login_at is None and admin.must_change_password \
                and not verify_password(settings.admin_password, admin.password_hash):
            admin.password_hash = hash_password(settings.admin_password)
            admin.must_change_password = False
            db.commit()
            log.warning("Admin password taken from SHIFTKEY_ADMIN_PASSWORD (account never used yet).")
            return "env-password"
        return None
    password = settings.admin_password or secrets.token_urlsafe(9)
    admin = User(username=settings.admin_username, full_name="Administrator", role=ROLE_ADMIN,
                 segment="all", password_hash=hash_password(password),
                 must_change_password=not settings.admin_password)
    db.add(admin)
    db.commit()
    banner = "=" * 70
    log.warning("\n%s\n  ShiftKey admin account created\n  username: %s\n  password: %s\n%s",
                banner, settings.admin_username, password, banner)
    return "created"
