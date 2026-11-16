"""Build engines for external databases, test them, browse schemas and run read-only SQL."""
from __future__ import annotations

import importlib
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

from ..config import settings
from ..models import Connection
from .crypto import decrypt

DIALECTS: Dict[str, Dict[str, Any]] = {
    "mssql": {"label": "Microsoft SQL Server", "driver": "pymssql", "module": "pymssql", "port": 1433,
              "hint": "Starlink Time Office usually runs on SQL Server. Database is e.g. 'TimeOffice' / 'StarLink'."},
    "mysql": {"label": "MySQL / MariaDB", "driver": "pymysql", "module": "pymysql", "port": 3306, "hint": ""},
    "postgresql": {"label": "PostgreSQL", "driver": "psycopg", "module": "psycopg", "port": 5432, "hint": ""},
    "sqlite": {"label": "SQLite file", "driver": None, "module": "sqlite3", "port": None,
               "hint": "Database = path to the .db file on the ShiftKey server."},
}

PARAM_NAMES = ("report_date", "date_from", "date_to")


def driver_available(dialect: str) -> bool:
    spec = DIALECTS.get(dialect)
    if not spec:
        return False
    try:
        importlib.import_module(spec["module"])
        return True
    except Exception:
        return False


def dialect_catalog() -> List[Dict[str, Any]]:
    return [{"key": k, "label": v["label"], "driver": v["driver"], "default_port": v["port"],
             "available": driver_available(k), "hint": v["hint"]} for k, v in DIALECTS.items()]


def build_url(conn: Connection, password: Optional[str] = None) -> URL | str:
    override = decrypt(conn.url_enc)
    if override:
        return override
    pw = password if password is not None else decrypt(conn.password_enc)
    spec = DIALECTS[conn.dialect]
    if conn.dialect == "sqlite":
        return f"sqlite:///{conn.database or ':memory:'}"
    drivername = f"{conn.dialect}+{spec['driver']}" if spec["driver"] else conn.dialect
    query = {k: str(v) for k, v in (conn.options or {}).items() if v not in (None, "")}
    return URL.create(drivername, username=conn.username or None, password=pw or None,
                      host=conn.host or None, port=conn.port or spec["port"], database=conn.database or None,
                      query=query)


def make_engine(conn: Connection, password: Optional[str] = None, timeout: int = 10) -> Engine:
    if not driver_available(conn.dialect):
        spec = DIALECTS.get(conn.dialect, {})
        raise RuntimeError(f"Driver '{spec.get('module', conn.dialect)}' is not installed on this server.")
    url = build_url(conn, password)
    connect_args: Dict[str, Any] = {}
    if conn.dialect == "mssql":
        connect_args = {"timeout": settings.sql_query_timeout, "login_timeout": timeout}
    elif conn.dialect == "mysql":
        connect_args = {"connect_timeout": timeout, "read_timeout": settings.sql_query_timeout}
    elif conn.dialect == "postgresql":
        connect_args = {"connect_timeout": timeout}
    elif conn.dialect == "sqlite":
        connect_args = {"timeout": timeout, "check_same_thread": False}
    return create_engine(url, poolclass=NullPool, connect_args=connect_args, future=True)


# ----------------------------------------------------------------------------

def _version_sql(dialect: str) -> str:
    return {"mssql": "SELECT @@VERSION", "mysql": "SELECT VERSION()", "postgresql": "SELECT version()",
            "sqlite": "SELECT sqlite_version()"}.get(dialect, "SELECT 1")


def test_connection(conn: Connection, password: Optional[str] = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        engine = make_engine(conn, password)
        with engine.connect() as c:
            version = c.execute(text(_version_sql(conn.dialect))).scalar()
            tables = len(inspect(c).get_table_names())
        version = " ".join(str(version or "").split())[:200]
        return {"ok": True, "message": f"Connected · {tables} tables", "server": version,
                "latency_ms": round((time.perf_counter() - t0) * 1000), "tables": tables}
    except Exception as exc:
        return {"ok": False, "message": _friendly(exc), "server": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000), "tables": None}


def _friendly(exc: Exception) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    msg = re.sub(r"\(Background on this error.*?\)", "", msg).strip()
    return msg[:400]


def list_tables(conn: Connection) -> List[Dict[str, Any]]:
    engine = make_engine(conn)
    with engine.connect() as c:
        insp = inspect(c)
        schemas: Sequence[Optional[str]] = [None]
        default_schema = insp.default_schema_name
        try:
            if conn.dialect in ("mssql", "postgresql"):
                schemas = [s for s in insp.get_schema_names()
                           if s not in ("information_schema", "pg_catalog", "pg_toast", "sys", "INFORMATION_SCHEMA",
                                        "guest", "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin",
                                        "db_backupoperator", "db_datareader", "db_datawriter", "db_denydatareader",
                                        "db_denydatawriter")]
        except Exception:
            schemas = [None]
        out: List[Dict[str, Any]] = []
        for schema in schemas:
            for name in insp.get_table_names(schema=schema):
                out.append({"name": name, "schema": schema if schema != default_schema else None, "type": "table"})
            try:
                for name in insp.get_view_names(schema=schema):
                    out.append({"name": name, "schema": schema if schema != default_schema else None, "type": "view"})
            except Exception:
                pass
    out.sort(key=lambda t: ((t["schema"] or ""), t["name"].lower()))
    return out


def table_columns(conn: Connection, table: str, schema: Optional[str] = None) -> List[Dict[str, Any]]:
    engine = make_engine(conn)
    with engine.connect() as c:
        insp = inspect(c)
        cols = insp.get_columns(table, schema=schema)
        try:
            pk = set(insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or [])
        except Exception:
            pk = set()
    return [{"name": col["name"], "type": str(col.get("type")), "nullable": bool(col.get("nullable", True)),
             "primary_key": col["name"] in pk} for col in cols]


def quote(conn: Connection, ident: str) -> str:
    return make_engine(conn).dialect.identifier_preparer.quote(ident)


def preview_table(conn: Connection, table: str, schema: Optional[str], limit: int) -> Dict[str, Any]:
    engine = make_engine(conn)
    prep = engine.dialect.identifier_preparer
    full = (prep.quote_schema(schema) + "." if schema else "") + prep.quote(table)
    if conn.dialect == "mssql":
        sql = f"SELECT TOP {int(limit) + 1} * FROM {full}"
    else:
        sql = f"SELECT * FROM {full} LIMIT {int(limit) + 1}"
    return run_select(conn, sql, {}, limit, audit_sql=sql)


# ----------------------------------------------------------------------------
# Read-only query execution
# ----------------------------------------------------------------------------

_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call|"
                        r"sp_\w*|xp_\w*)\b|\binto\s+[\w\[#@\"`]", re.IGNORECASE)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def validate_readonly(sql: str) -> str:
    body = strip_comments(sql).rstrip(";").strip()
    if not body:
        raise ValueError("The query is empty.")
    if ";" in body:
        raise ValueError("Only a single statement is allowed.")
    first = body.split(None, 1)[0].lower()
    if first not in ("select", "with", "show", "describe", "desc", "explain", "pragma"):
        raise ValueError("Only SELECT queries are allowed here.")
    if _FORBIDDEN.search(body):
        raise ValueError("The query contains a keyword that is not allowed in read-only mode.")
    return body


def prepare_sql(sql: str) -> str:
    """Keep :report_date / :date_from / :date_to as bind parameters and escape every other
    colon (e.g. '08:30') so SQLAlchemy does not mistake it for a parameter."""
    names = "|".join(PARAM_NAMES)
    placeholder = "\x00"
    sql = re.sub(rf"(?<![:\w]):({names})\b", lambda m: placeholder + m.group(1), sql)
    sql = sql.replace("::", "\x01")      # keep Postgres casts
    sql = sql.replace(":", "\\:")
    sql = sql.replace("\x01", "::").replace(placeholder, ":")
    return sql


def referenced_params(sql: str) -> List[str]:
    return [p for p in PARAM_NAMES if re.search(rf"(?<![:\w]):{p}\b", sql)]


def _coerce(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def run_select(conn: Connection, sql: str, params: Optional[Dict[str, Any]], limit: int,
               audit_sql: Optional[str] = None, enforce_readonly: bool = True) -> Dict[str, Any]:
    body = validate_readonly(sql) if enforce_readonly else strip_comments(sql).rstrip(";")
    stmt = text(prepare_sql(body))
    bind = {k: v for k, v in (params or {}).items() if k in referenced_params(body)}
    t0 = time.perf_counter()
    engine = make_engine(conn)
    with engine.connect() as c:
        trans = c.begin()
        try:
            result = c.execute(stmt, bind)
            columns = list(result.keys())
            fetched = result.fetchmany(limit + 1) if limit else result.fetchall()
        finally:
            trans.rollback()   # never persist anything from here
    truncated = bool(limit) and len(fetched) > limit
    rows = [[_coerce(v) for v in r] for r in (fetched[:limit] if limit else fetched)]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000), "sql": audit_sql or body}


def fetch_all(conn: Connection, sql: str, params: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Unlimited read used by pull jobs. Returns a list of dicts (raw python values kept)."""
    body = validate_readonly(sql)
    stmt = text(prepare_sql(body))
    bind = {k: v for k, v in (params or {}).items() if k in referenced_params(body)}
    engine = make_engine(conn)
    with engine.connect() as c:
        trans = c.begin()
        try:
            result = c.execute(stmt, bind)
            cols = list(result.keys())
            rows = [dict(zip(cols, r)) for r in result.fetchall()]
        finally:
            trans.rollback()
    return rows
