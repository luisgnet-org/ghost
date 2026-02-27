"""
Configuration management for ghost.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("ghost")


# Paths
GHOST_AGENCY_DIR = Path(__file__).parent
PROJECT_ROOT = GHOST_AGENCY_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
RUNS_DIR = Path.home() / "ghost" / "ghost_run_dir"
WORKFLOWS_DIR = RUNS_DIR / "workflows"
STATE_PATH = RUNS_DIR / "state.json"
TELEGRAM_DB_PATH = RUNS_DIR / "telegram" / "telegram.db"


def workflow_dir(name: str) -> Path:
    """Get the runs directory for a specific workflow. Creates it if needed."""
    d = WORKFLOWS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict:
    """Load configuration from YAML."""
    try:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception as e:
        print(f"[config] Failed to load: {e}")
    return {"jobs": []}


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get environment variable."""
    return os.environ.get(key, default)


def get_llm_config() -> dict:
    """Get LLM configuration from environment."""
    return {
        "api_key": get_env("LLM_API_KEY"),
        "base_url": get_env("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        "model": get_env("LLM_MODEL", "llama-3.3-70b-versatile"),
    }


def get_telegram_config() -> dict:
    """Get Telegram configuration from environment."""
    return {
        "bot_token": get_env("TELEGRAM_BOT_TOKEN"),
        "chat_id": get_env("TELEGRAM_CHAT_ID"),
    }


def get_transcription_config() -> dict:
    """Get audio transcription configuration (Groq Whisper API)."""
    return {
        "api_key": get_env("GROQ_API_KEY") or get_env("LLM_API_KEY"),
        "model": "whisper-large-v3-turbo",
        "endpoint": "https://api.groq.com/openai/v1/audio/transcriptions",
    }


def get_toggl_config() -> dict:
    """Get Toggl configuration from environment."""
    return {
        "api_token": get_env("TOGGL_API_TOKEN"),
    }


def set_not_before(job_name: str, until_dt: datetime) -> None:
    """Suppress a job until the given datetime by writing to state.json."""
    try:
        state = {}
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
        if "not_before" not in state:
            state["not_before"] = {}
        state["not_before"][job_name] = until_dt.isoformat()
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
        logger.info(f"set_not_before: {job_name} until {until_dt.isoformat()}")
    except Exception as e:
        logger.error(f"set_not_before failed: {e}")


# --- Shared workflow state ---
# Workflows publish values here for other workflows to read.
# Values are stored as naive local-time ISO strings.

def set_shared(key: str, value) -> None:
    """Set a shared value in state.json["shared"]. Converts datetimes to local naive ISO."""
    try:
        state = {}
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
        if "shared" not in state:
            state["shared"] = {}
        # Convert tz-aware datetimes to local naive
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone().replace(tzinfo=None)
            value = value.isoformat()
        state["shared"][key] = value
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        logger.error(f"set_shared({key}) failed: {e}")


def get_shared(key: str, default=None):
    """Read a shared value from state.json["shared"]."""
    try:
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
            return state.get("shared", {}).get(key, default)
    except Exception:
        pass
    return default


def emit_event(event_name: str) -> None:
    """Emit an event for the daemon to consume. Daemon dispatches on_<event_name> schedules."""
    try:
        state = {}
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
        events = state.get("events", [])
        events.append(event_name)
        state["events"] = events
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
        logger.info(f"emit_event: {event_name}")
    except Exception as e:
        logger.error(f"emit_event({event_name}) failed: {e}")
