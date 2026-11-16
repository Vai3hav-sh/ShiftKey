"""Database models."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------
# Users & sessions
# ----------------------------------------------------------------------------

ROLE_ADMIN, ROLE_HOD, ROLE_HR = "admin", "hod", "hr"
ROLES = (ROLE_ADMIN, ROLE_HOD, ROLE_HR)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # HR scope: "indian" | "local" | "all". Ignored for other roles.
    segment: Mapped[str] = mapped_column(String(10), nullable=False, default="all")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    departments: Mapped[list["UserDepartment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def dept_codes(self) -> list[str]:
        return sorted(d.dept_code for d in self.departments)


class UserDepartment(Base):
    __tablename__ = "user_departments"
    __table_args__ = (UniqueConstraint("user_id", "dept_code", name="uq_user_dept"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dept_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    user: Mapped[User] = relationship(back_populates="departments")


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(String(200))
    ip: Mapped[Optional[str]] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")


# ----------------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------------

class Department(Base):
    __tablename__ = "departments"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_seen: Mapped[Optional[date]] = mapped_column(Date)
    headcount: Mapped[int] = mapped_column(Integer, default=0)


class Shift(Base):
    """Shift master. Starlink prints the shift code and start time only; the end
    time is what HODs fall back to when resolving a missing OUT punch."""
    __tablename__ = "shifts"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(60))
    start: Mapped[Optional[str]] = mapped_column(String(5))
    end: Mapped[Optional[str]] = mapped_column(String(5))
    source: Mapped[str] = mapped_column(String(10), default="seed")  # seed | import | admin
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("report_date", name="uq_reports_report_date"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    company: Mapped[Optional[str]] = mapped_column(String(120))
    title: Mapped[Optional[str]] = mapped_column(String(200))
    run_at: Mapped[Optional[str]] = mapped_column(String(40))
    source_filename: Mapped[Optional[str]] = mapped_column(String(255))
    source_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    source_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    source_kind: Mapped[Optional[str]] = mapped_column(String(10))  # upload | sql
    sync_job_id: Mapped[Optional[int]] = mapped_column(Integer)
    sync_run_id: Mapped[Optional[int]] = mapped_column(Integer)
    source_columns: Mapped[Optional[dict]] = mapped_column(JSON)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    total: Mapped[int] = mapped_column(Integer, default=0)
    present: Mapped[int] = mapped_column(Integer, default=0)
    absent: Mapped[int] = mapped_column(Integer, default=0)
    halfday: Mapped[int] = mapped_column(Integer, default=0)
    mispunch: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)

    records: Mapped[list["AttendanceRecord"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="AttendanceRecord.seq"
    )


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    dept_code: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    dept_name: Mapped[Optional[str]] = mapped_column(String(120))
    paycode: Mapped[str] = mapped_column(String(40), index=True)
    segment: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    card_no: Mapped[Optional[str]] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(160))
    shift: Mapped[Optional[str]] = mapped_column(String(20))
    shift_start: Mapped[Optional[str]] = mapped_column(String(5))
    time_in: Mapped[Optional[str]] = mapped_column(String(5))
    lunch_out: Mapped[Optional[str]] = mapped_column(String(5))
    lunch_in: Mapped[Optional[str]] = mapped_column(String(5))
    time_out: Mapped[Optional[str]] = mapped_column(String(5))
    hrs: Mapped[Optional[str]] = mapped_column(String(10))
    hrs_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), index=True)
    early: Mapped[Optional[str]] = mapped_column(String(10))
    late: Mapped[Optional[str]] = mapped_column(String(10))
    shift_early: Mapped[Optional[str]] = mapped_column(String(10))
    excess_lunch: Mapped[Optional[str]] = mapped_column(String(10))
    ot: Mapped[Optional[str]] = mapped_column(String(10))
    ot_amount: Mapped[Optional[str]] = mapped_column(String(20))
    os: Mapped[Optional[str]] = mapped_column(String(10))
    manual: Mapped[bool] = mapped_column(Boolean, default=False)

    is_present: Mapped[bool] = mapped_column(Boolean, default=False)
    is_absent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_halfday: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_mispunch: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_night: Mapped[bool] = mapped_column(Boolean, default=False)
    has_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[list]] = mapped_column(JSON)
    extra: Mapped[Optional[dict]] = mapped_column(JSON)
    raw: Mapped[Optional[str]] = mapped_column(Text)

    # Active correction (denormalised from the latest Correction row).
    corrected_in: Mapped[Optional[str]] = mapped_column(String(5))
    corrected_out: Mapped[Optional[str]] = mapped_column(String(5))
    corrected_status: Mapped[Optional[str]] = mapped_column(String(10))
    corrected_hrs_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    corrected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    corrected_by_id: Mapped[Optional[int]] = mapped_column(Integer)
    correction_remark: Mapped[Optional[str]] = mapped_column(String(300))

    report: Mapped[Report] = relationship(back_populates="records")
    corrections: Mapped[list["Correction"]] = relationship(
        back_populates="record", cascade="all, delete-orphan", order_by="Correction.id"
    )

    @property
    def is_corrected(self) -> bool:
        return self.corrected_at is not None


class Correction(Base):
    """Audit trail of every resolution / revert applied to a record."""
    __tablename__ = "corrections"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("attendance_records.id", ondelete="CASCADE"), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # shift_time | set_time | halfday | absent | revert
    field: Mapped[Optional[str]] = mapped_column(String(5))          # in | out
    original_in: Mapped[Optional[str]] = mapped_column(String(5))
    original_out: Mapped[Optional[str]] = mapped_column(String(5))
    original_status: Mapped[Optional[str]] = mapped_column(String(10))
    corrected_in: Mapped[Optional[str]] = mapped_column(String(5))
    corrected_out: Mapped[Optional[str]] = mapped_column(String(5))
    corrected_status: Mapped[Optional[str]] = mapped_column(String(10))
    corrected_hrs_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    remark: Mapped[Optional[str]] = mapped_column(String(300))
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_name: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    record: Mapped[AttendanceRecord] = relationship(back_populates="corrections")


# ----------------------------------------------------------------------------
# Audit trail (append-only, hash-chained)
# ----------------------------------------------------------------------------

class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)

    actor_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    actor_username: Mapped[Optional[str]] = mapped_column(String(60))
    actor_name: Mapped[Optional[str]] = mapped_column(String(120))
    actor_role: Mapped[Optional[str]] = mapped_column(String(10))
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(200))

    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="success")
    summary: Mapped[str] = mapped_column(String(400), nullable=False)

    target_type: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    target_label: Mapped[Optional[str]] = mapped_column(String(200))
    report_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    report_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    dept_code: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    segment: Mapped[Optional[str]] = mapped_column(String(10))
    paycode: Mapped[Optional[str]] = mapped_column(String(40), index=True)

    before: Mapped[Optional[dict]] = mapped_column(JSON)
    after: Mapped[Optional[dict]] = mapped_column(JSON)
    changes: Mapped[Optional[list]] = mapped_column(JSON)
    meta: Mapped[Optional[dict]] = mapped_column(JSON)

    prev_hash: Mapped[Optional[str]] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


# ----------------------------------------------------------------------------
# SQL integrations: external connections, sync jobs, run history
# ----------------------------------------------------------------------------

class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)  # mssql | mysql | postgresql | sqlite
    host: Mapped[Optional[str]] = mapped_column(String(200))
    port: Mapped[Optional[int]] = mapped_column(Integer)
    database: Mapped[Optional[str]] = mapped_column(String(300))
    username: Mapped[Optional[str]] = mapped_column(String(120))
    password_enc: Mapped[Optional[str]] = mapped_column(Text)
    url_enc: Mapped[Optional[str]] = mapped_column(Text)        # full SQLAlchemy URL override (encrypted)
    options: Mapped[Optional[dict]] = mapped_column(JSON)       # driver query options
    read_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    last_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    last_test_message: Mapped[Optional[str]] = mapped_column(String(400))
    server_info: Mapped[Optional[str]] = mapped_column(String(300))

    jobs: Mapped[list["SyncJob"]] = relationship(back_populates="connection", cascade="all, delete-orphan")


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)  # pull | push
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(300))

    # pull
    mode: Mapped[Optional[str]] = mapped_column(String(10))       # daily | punches
    sql: Mapped[Optional[str]] = mapped_column(Text)              # daily rows / punch log query
    employees_sql: Mapped[Optional[str]] = mapped_column(Text)    # employee master (punch mode)
    mapping: Mapped[Optional[dict]] = mapped_column(JSON)         # shiftkey field -> source column
    employees_mapping: Mapped[Optional[dict]] = mapped_column(JSON)
    options: Mapped[Optional[dict]] = mapped_column(JSON)         # hrs_format, status_map, day_start, ...

    # push
    dataset: Mapped[Optional[str]] = mapped_column(String(20))    # attendance | corrections | audit_events | reports
    target_table: Mapped[Optional[str]] = mapped_column(String(120))
    target_schema: Mapped[Optional[str]] = mapped_column(String(60))
    key_fields: Mapped[Optional[list]] = mapped_column(JSON)
    column_map: Mapped[Optional[dict]] = mapped_column(JSON)      # shiftkey field -> target column (subset)
    create_table: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    push_mode: Mapped[str] = mapped_column(String(10), default="upsert", nullable=False)  # upsert | update | insert
    auto_push: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # schedule
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_time: Mapped[Optional[str]] = mapped_column(String(5))  # HH:MM local server time
    schedule_offset_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schedule_last_date: Mapped[Optional[date]] = mapped_column(Date)

    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_status: Mapped[Optional[str]] = mapped_column(String(10))
    last_message: Mapped[Optional[str]] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    connection: Mapped[Connection] = relationship(back_populates="jobs")
    runs: Mapped[list["SyncRun"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sync_jobs.id", ondelete="SET NULL"), index=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)  # manual | schedule | auto | preview
    status: Mapped[str] = mapped_column(String(10), default="running", nullable=False)  # running | success | partial | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    report_ids: Mapped[Optional[list]] = mapped_column(JSON)
    message: Mapped[Optional[str]] = mapped_column(Text)
    log: Mapped[Optional[list]] = mapped_column(JSON)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer)
    actor_username: Mapped[Optional[str]] = mapped_column(String(60))

    job: Mapped[Optional[SyncJob]] = relationship(back_populates="runs")
