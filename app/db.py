"""SQLAlchemy engine, session helpers and a tiny additive migration step."""
from __future__ import annotations

import logging
import os
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger("shiftkey.db")


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        path = url.replace("sqlite:///", "", 1)
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


# Columns added after the first release. create_all() never alters existing tables,
# so we add missing columns here (both SQLite and Postgres accept this DDL).
ADDED_COLUMNS = {
    "user_sessions": [("ip", "VARCHAR(64)")],
    "reports": [("source_sha256", "VARCHAR(64)"), ("source_kind", "VARCHAR(10)"), ("sync_job_id", "INTEGER"),
                ("sync_run_id", "INTEGER")],
    "attendance_records": [
        ("segment", "VARCHAR(10)"),
        ("corrected_in", "VARCHAR(5)"),
        ("corrected_out", "VARCHAR(5)"),
        ("corrected_status", "VARCHAR(10)"),
        ("corrected_hrs_minutes", "INTEGER"),
        ("corrected_at", "TIMESTAMP"),
        ("corrected_by_id", "INTEGER"),
        ("correction_remark", "VARCHAR(300)"),
    ],
}


MIGRATED: list = []  # (table, column) pairs added during this start-up, for the audit trail


def migrate() -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in existing:
                    log.info("migrate: adding %s.%s", table, name)
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    MIGRATED.append(f"{table}.{name}")


def init_db() -> None:
    from . import models  # noqa: F401  (registers tables)

    migrate()
    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
