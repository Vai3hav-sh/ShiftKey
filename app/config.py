"""Application settings, all overridable through environment variables."""
from __future__ import annotations

from typing import Dict

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHIFTKEY_", env_file=".env", extra="ignore")

    # SQLAlchemy URL. Postgres in docker-compose, SQLite when running locally.
    database_url: str = "sqlite:///./data/shiftkey.db"

    # Used for the export filename: "21 October 2026 Expatriates.xlsx".
    report_name_suffix: str = "Expatriates"

    # A single "P" record spanning more than this many hours is almost always a
    # missed punch that the machine paired with the next day's punch.
    long_span_hours: float = 16.0

    # Maximum accepted upload size in megabytes.
    max_upload_mb: int = 20

    # PayCode prefix -> employee segment. "1:indian,2:local"
    segment_prefixes: str = "1:indian,2:local"

    # --- authentication -------------------------------------------------
    admin_username: str = "admin"
    # Leave empty to have a random password generated and printed in the logs on first start.
    admin_password: str = ""
    session_hours: int = 24 * 7
    # Set to true when ShiftKey is served over HTTPS.
    secure_cookies: bool = False
    login_max_failures: int = 6
    login_lockout_minutes: int = 10

    # --- integrations ---------------------------------------------------
    # Fernet key for connection secrets. Leave empty to keep an auto-generated key in data_dir.
    secret_key: str = ""
    data_dir: str = "./data"
    scheduler_enabled: bool = True
    # Run auto-push / scheduled jobs in background threads (False = inline, used by tests).
    sync_async: bool = True
    sql_query_timeout: int = 60
    sql_preview_rows: int = 50

    def segment_map(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for part in self.segment_prefixes.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                out[k.strip()] = v.strip().lower()
        return out


settings = Settings()

SEGMENT_LABELS = {"indian": "Indian", "local": "Local", "other": "Other", "all": "All"}
