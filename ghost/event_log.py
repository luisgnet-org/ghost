"""Lightweight lifecycle event logger.

Appends timestamped events to a JSONL file for tracing and diagnostics.
"""

import json
import time
from pathlib import Path

from ghost.config import RUNS_DIR

EVENTS_FILE = RUNS_DIR / "events.jsonl"
_MAX_LINES = 5000
_KEEP = 3000


def _maybe_trim():
    try:
        if not EVENTS_FILE.exists():
            return
        lines = EVENTS_FILE.read_text().splitlines()
        if len(lines) > _MAX_LINES:
            EVENTS_FILE.write_text("\n".join(lines[-_KEEP:]) + "\n")
    except Exception:
        pass


def log_event(event: str, **meta):
    """Append a lifecycle event."""
    entry = {"ts": time.time(), "event": event}
    if meta:
        entry["meta"] = {k: v for k, v in meta.items() if v is not None}
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _maybe_trim()
    except Exception:
        pass


def read_events(since_ts: float = 0, limit: int = 1000) -> list[dict]:
    """Read events since a unix timestamp."""
    if not EVENTS_FILE.exists():
        return []
    events = []
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("ts", 0) >= since_ts:
                        events.append(e)
                except json.JSONDecodeError:
                    continue
        return events[-limit:]
    except Exception:
        return []
