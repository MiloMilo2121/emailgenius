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
    tavily_api_key: str | None = None
    remote_browser_ws_url: str | None = None
    workspace_folder_id: str | None = None
    drive_poll_interval_seconds: int = 60
    io_mode: str = "local"
    openai_fallback_api_key: str | None = None
    openai_fallback_base_url: str | None = None
    openai_fallback_chat_model: str | None = None
    exa_api_key: str | None = None
    research_model: str = "deepseek/deepseek-chat-v3.1"
    writer_model: str = "anthropic/claude-3.5-haiku"
    writer_fallback_model: str | None = None


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
        openrouter_api_key = _env_or_none("OPENROUTER_API_KEY")
        openai_api_key = _env_or_none("OPENAI_API_KEY", "EMAILGENIUS_OPENAI_API_KEY", "OPENROUTER_API_KEY")
        
        # If the resolved openai API key is from OpenRouter (either extracted directly or visibly prefixed)
        is_openrouter = openrouter_api_key or (openai_api_key and openai_api_key.startswith("sk-or-"))
        openrouter_base_url = _env_or_none("OPENROUTER_BASE_URL") or (
            "https://openrouter.ai/api/v1" if is_openrouter else None
        )
        
        try:
            retention_raw = os.getenv("EMAILGENIUS_RETENTION_DAYS", "90").strip()
            retention_days = int(retention_raw)
        except ValueError:
            retention_days = 90

        return cls(
            database_url=_default_db_url(),
            openai_api_key=openai_api_key,
            openai_base_url=_env_or_none("OPENAI_BASE_URL", "EMAILGENIUS_OPENAI_BASE_URL", "OPENROUTER_BASE_URL")
            or openrouter_base_url,
            tavily_api_key=_env_or_none("TAVILY_API_KEY"),
            remote_browser_ws_url=_env_or_none("REMOTE_BROWSER_WS_URL"),
            openai_chat_model=os.getenv("EMAILGENIUS_OPENAI_CHAT_MODEL", "gpt-5"),
            openai_embedding_model=os.getenv(
                "EMAILGENIUS_OPENAI_EMBED_MODEL",
                "text-embedding-3-small",
            ),
            openai_fallback_api_key=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_API_KEY"),
            openai_fallback_base_url=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_BASE_URL"),
            openai_fallback_chat_model=_env_or_none("EMAILGENIUS_OPENAI_FALLBACK_CHAT_MODEL"),
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
            retention_days=retention_days,
            workspace_folder_id=_env_or_none("EMAILGENIUS_WORKSPACE_FOLDER_ID"),
            drive_poll_interval_seconds=poll_seconds,
            io_mode=io_mode,
            exa_api_key=_env_or_none("EXA_API_KEY", "EMAILGENIUS_EXA_API_KEY"),
            research_model=os.getenv("EMAILGENIUS_RESEARCH_MODEL", "deepseek/deepseek-chat-v3.1").strip()
            or "deepseek/deepseek-chat-v3.1",
            writer_model=os.getenv("EMAILGENIUS_WRITER_MODEL", "anthropic/claude-3.5-haiku").strip()
            or "anthropic/claude-3.5-haiku",
            writer_fallback_model=_env_or_none("EMAILGENIUS_WRITER_FALLBACK_MODEL"),
        )


def app_home() -> Path:
    root = Path(os.getenv("EMAILGENIUS_HOME", ".emailgenius")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root
