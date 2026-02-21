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
    openai_fallback_api_key: str | None = None
    openai_fallback_base_url: str | None = None
    openai_fallback_chat_model: str | None = None


    @classmethod
    def from_env(cls) -> "AppConfig":
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
        )


def app_home() -> Path:
    root = Path(os.getenv("EMAILGENIUS_HOME", ".emailgenius")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root
