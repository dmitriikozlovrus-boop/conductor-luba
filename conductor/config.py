from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    openai_api_key: str
    openai_model: str
    openai_transcribe_model: str
    openai_transcribe_fallback_model: str
    notion_token: str
    notion_tasks_database_id: str
    notion_study_database_id: str
    notion_projects_database_id: str
    host: str
    port: int
    confidence_threshold: float
    pending_store_path: str
    timezone: str
    todoist_enabled: bool
    todoist_api_token: str


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        openai_transcribe_model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        openai_transcribe_fallback_model=os.getenv("OPENAI_TRANSCRIBE_FALLBACK_MODEL", "whisper-1"),
        notion_token=os.getenv("NOTION_TOKEN", ""),
        notion_tasks_database_id=os.getenv("NOTION_TASKS_DATABASE_ID", ""),
        notion_study_database_id=os.getenv("NOTION_STUDY_DATABASE_ID", ""),
        notion_projects_database_id=os.getenv("NOTION_PROJECTS_DATABASE_ID", ""),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.70")),
        pending_store_path=os.getenv("PENDING_STORE_PATH", "data/pending.json"),
        timezone=os.getenv("TIMEZONE", "America/Mexico_City"),
        todoist_enabled=_bool("TODOIST_ENABLED", False),
        todoist_api_token=os.getenv("TODOIST_API_TOKEN", ""),
    )
