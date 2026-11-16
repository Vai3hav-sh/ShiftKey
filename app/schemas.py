"""Request bodies."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=1, max_length=200)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(default="", max_length=120)
    role: str
    password: str = Field(min_length=1, max_length=200)
    segment: str = "all"
    departments: List[str] = []
    must_change_password: bool = True


class UserUpdateIn(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=120)
    role: Optional[str] = None
    segment: Optional[str] = None
    departments: Optional[List[str]] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=200)
    must_change_password: Optional[bool] = None


class ShiftIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    start: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class CorrectionIn(BaseModel):
    action: str
    field: Optional[str] = None
    time: Optional[str] = Field(default=None, max_length=5)
    remark: Optional[str] = Field(default=None, max_length=300)


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    dialect: str
    host: Optional[str] = Field(default=None, max_length=200)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    database: Optional[str] = Field(default=None, max_length=300)
    username: Optional[str] = Field(default=None, max_length=120)
    password: Optional[str] = Field(default=None, max_length=300)   # None = keep existing
    url: Optional[str] = Field(default=None, max_length=1000)       # full SQLAlchemy URL override
    options: dict = {}
    read_only: bool = True
    notes: Optional[str] = Field(default=None, max_length=300)


class QueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    params: dict = {}
    limit: int = Field(default=200, ge=1, le=5000)


class SyncJobIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    direction: str
    connection_id: int
    enabled: bool = True
    description: Optional[str] = Field(default=None, max_length=300)
    mode: Optional[str] = None
    sql: Optional[str] = Field(default=None, max_length=20000)
    employees_sql: Optional[str] = Field(default=None, max_length=20000)
    mapping: Optional[dict] = None
    employees_mapping: Optional[dict] = None
    options: Optional[dict] = None
    dataset: Optional[str] = None
    target_table: Optional[str] = Field(default=None, max_length=120)
    target_schema: Optional[str] = Field(default=None, max_length=60)
    key_fields: Optional[List[str]] = None
    column_map: Optional[dict] = None
    create_table: bool = True
    push_mode: str = "upsert"
    auto_push: bool = False
    schedule_enabled: bool = False
    schedule_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    schedule_offset_days: int = Field(default=1, ge=0, le=31)


class PreviewIn(BaseModel):
    connection_id: int
    mode: str = "daily"
    sql: str = Field(min_length=1, max_length=20000)
    employees_sql: Optional[str] = Field(default=None, max_length=20000)
    mapping: Optional[dict] = None
    employees_mapping: Optional[dict] = None
    options: Optional[dict] = None
    date: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=500)


class RunIn(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
