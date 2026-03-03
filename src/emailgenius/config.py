from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_db_url() -> str:
    return os.getenv(
        "EMAILGENIUS_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/emailgenius",
    )


def _env_or_none(*names: str) -> str | None:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        value = raw.strip()
        if value:
            return value
    return None


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_url: str
    openai_api_key: str | None
    openai_base_url: str | None
    openai_chat_model: str
    openai_embedding_model: str
    google_service_account_json: str | None
    retention_days: int
    workspace_folder_id: str | None = None
    drive_poll_interval_seconds: int = 60
    io_mode: str = "local"
    openai_fallback_api_key: str | None = None
    openai_fallback_base_url: str | None = None
    openai_fallback_chat_model: str | None = None


    @classmethod
    def from_env(cls) -> "AppConfig":
        poll_seconds_raw = (os.getenv("EMAILGENIUS_DRIVE_POLL_INTERVAL_SECONDS") or "60").strip()
        try:
            poll_seconds = max(5, int(poll_seconds_raw))
        except ValueError:
            poll_seconds = 60
        io_mode = (os.getenv("EMAILGENIUS_IO_MODE") or "local").strip().lower() or "local"
        if io_mode not in {"local", "drive"}:
            io_mode = "local"
        return cls(
            database_url=_default_db_url(),
            openai_api_key=_env_or_none("OPENAI_API_KEY", "EMAILGENIUS_OPENAI_API_KEY"),
            openai_base_url=_env_or_none("OPENAI_BASE_URL", "EMAILGENIUS_OPENAI_BASE_URL"),
            openai_chat_model=os.getenv("EMAILGENIUS_OPENAI_CHAT_MODEL", "gpt-5"),
            openai_embedding_model=os.getenv(
                "EMAILGENIUS_OPENAI_EMBED_MODEL",
                "text-embedding-3-small",
            ),
            openai_fallback_api_key=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_API_KEY"),
            openai_fallback_base_url=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_BASE_URL"),
            openai_fallback_chat_model=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_CHAT_MODEL"),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
            retention_days=int(os.getenv("EMAILGENIUS_RETENTION_DAYS", "90")),
            workspace_folder_id=_env_or_none("EMAILGENIUS_WORKSPACE_FOLDER_ID"),
            drive_poll_interval_seconds=poll_seconds,
            io_mode=io_mode,
        )


def app_home() -> Path:
    root = Path(os.getenv("EMAILGENIUS_HOME", ".emailgenius")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root
