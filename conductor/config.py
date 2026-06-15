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
    notion_streams_database_id: str
    host: str
    port: int
    confidence_threshold: float
    pending_store_path: str
    recent_store_path: str
    timezone: str
    todoist_enabled: bool
    todoist_api_token: str
    todoist_webhook_secret: str
    todoist_sync_interval_seconds: int
    todoist_sync_on_start: bool
    todoist_sync_paused: bool
    todoist_sync_mode: str
    todoist_allow_project_create: bool
    todoist_allow_task_create: bool
    todoist_allow_task_move: bool
    todoist_allow_label_write: bool
    todoist_allow_status_write: bool
    todoist_allow_missing_cancel: bool
    todoist_max_task_moves: int
    todoist_snapshot_path: str
    todoist_sync_state_path: str
    task_sync_secret: str
    todoist_completed_since: str


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
        notion_streams_database_id=os.getenv("NOTION_STREAMS_DATABASE_ID", ""),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.70")),
        pending_store_path=os.getenv("PENDING_STORE_PATH", "data/pending.json"),
        recent_store_path=os.getenv("RECENT_STORE_PATH", "data/recent.json"),
        timezone=os.getenv("TIMEZONE", "America/Mexico_City"),
        todoist_enabled=_bool("TODOIST_ENABLED", False),
        todoist_api_token=os.getenv("TODOIST_API_TOKEN", ""),
        todoist_webhook_secret=os.getenv("TODOIST_WEBHOOK_SECRET", ""),
        todoist_sync_interval_seconds=int(os.getenv("TODOIST_SYNC_INTERVAL_SECONDS", "300")),
        todoist_sync_on_start=_bool("TODOIST_SYNC_ON_START", True),
        todoist_sync_paused=_bool("TODOIST_SYNC_PAUSED", False),
        todoist_sync_mode=os.getenv("TODOIST_SYNC_MODE", "observe").strip().lower(),
        todoist_allow_project_create=_bool("TODOIST_ALLOW_PROJECT_CREATE", False),
        todoist_allow_task_create=_bool("TODOIST_ALLOW_TASK_CREATE", False),
        todoist_allow_task_move=_bool("TODOIST_ALLOW_TASK_MOVE", False),
        todoist_allow_label_write=_bool("TODOIST_ALLOW_LABEL_WRITE", False),
        todoist_allow_status_write=_bool("TODOIST_ALLOW_STATUS_WRITE", False),
        todoist_allow_missing_cancel=_bool("TODOIST_ALLOW_MISSING_CANCEL", False),
        todoist_max_task_moves=int(os.getenv("TODOIST_MAX_TASK_MOVES", "10")),
        todoist_snapshot_path=os.getenv("TODOIST_SNAPSHOT_PATH", "data/todoist_inventory_snapshot.json"),
        todoist_sync_state_path=os.getenv("TODOIST_SYNC_STATE_PATH", "data/todoist_sync_state.json"),
        task_sync_secret=os.getenv("TASK_SYNC_SECRET", ""),
        todoist_completed_since=os.getenv("TODOIST_COMPLETED_SINCE", "2007-01-01"),
    )
