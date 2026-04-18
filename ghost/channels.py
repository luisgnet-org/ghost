"""channels.py — Append-only JSONL channel messaging primitives.

The language for channel operations. Business logic lives elsewhere.

Primitives:
    write(channel, text, from_id, source, type, **meta) → event_id
    read(channel, offset) → (events, new_offset)
    poll(channels, timeout, cursors) → (messages, cursors)  [async]
    init_cursors(channels) → cursors  (seek to EOF for each)
    messages_only(events) → filtered to type="message"
"""

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

from ghost.config import RUNS_DIR

logger = logging.getLogger("ghost")

CHANNELS_DIR = RUNS_DIR / "channels"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("..", "_").replace(" ", "_").lower()


def path(channel: str) -> Path:
    """Get the JSONL file path for a channel. Creates dir if needed."""
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    return CHANNELS_DIR / f"{_safe(channel)}.jsonl"


def write(channel: str, text: str, from_id: str, source: str = "daemon",
          event_type: str = "message", **meta) -> str | None:
    """Append an event. Returns event_id or None. Never raises."""
    try:
        p = path(channel)
        ts = time.time()
        eid = f"{event_type}_{int(ts * 1000)}_{secrets.token_hex(2)}"
        event = {"ts": ts, "id": eid, "type": event_type,
                 "from": from_id, "source": source, "text": text}
        if meta:
            event["meta"] = meta
        with open(p, "a") as f:
            f.write(json.dumps(event) + "\n")
        return eid
    except Exception as e:
        logger.debug(f"channel write failed ({channel}): {e}")
        return None


def read(channel: str, offset: int = 0) -> tuple[list[dict], int]:
    """Read events from offset. Returns (events, new_offset)."""
    p = path(channel)
    if not p.exists():
        return [], 0
    try:
        with open(p, "r") as f:
            f.seek(0, 2)
            end = f.tell()
            if end <= offset:
                return [], offset
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
    except Exception:
        return [], offset

    events = []
    for line in data.splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass
    return events, new_offset


def init_cursors(channels: list[str]) -> dict[str, int]:
    """Initialize cursors to EOF for each channel. New channels start at end."""
    cursors = {}
    for ch in channels:
        p = path(ch)
        cursors[ch] = p.stat().st_size if p.exists() else 0
    return cursors


def messages_only(events: list[dict], skip_sources: set | None = None) -> list[dict]:
    """Filter to message-type events, optionally skipping certain sources."""
    skip = skip_sources or set()
    return [e for e in events if e.get("type") == "message" and e.get("source") not in skip]


async def poll(channels: list[str], timeout: float, cursors: dict[str, int],
               skip_sources: set | None = None) -> tuple[list[dict], dict[str, int]]:
    """Async poll for new messages across channels. Returns (messages, updated_cursors)."""
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return [], cursors

        all_msgs = []
        for ch in channels:
            events, new_offset = read(ch, cursors.get(ch, 0))
            if events:
                cursors[ch] = new_offset
                for m in messages_only(events, skip_sources):
                    m["_channel"] = ch
                    all_msgs.append(m)

        if all_msgs:
            return all_msgs, cursors

        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

CURSORS_DIR = CHANNELS_DIR / "cursors"


def cursor_path(channel: str, agent: str) -> Path:
    """Path to cursor file: channels/cursors/{channel}.{agent}.cursor"""
    CURSORS_DIR.mkdir(parents=True, exist_ok=True)
    return CURSORS_DIR / f"{_safe(channel)}.{_safe(agent)}.cursor"


def load_cursor(channel: str, agent: str) -> int:
    """Load byte offset from disk. Returns 0 if no cursor."""
    p = cursor_path(channel, agent)
    try:
        return int(p.read_text().strip()) if p.exists() else 0
    except (ValueError, OSError):
        return 0


def save_cursor(channel: str, agent: str, offset: int):
    """Save byte offset to disk."""
    cursor_path(channel, agent).write_text(str(offset))


def rewind_cursor(channel: str, agent: str, to_ts: float):
    """Rewind cursor to the byte offset of the last event before to_ts."""
    p = path(channel)
    if not p.exists():
        return

    best_offset = 0
    current_offset = 0
    for line in p.read_text().splitlines(keepends=True):
        if line.strip():
            try:
                event = json.loads(line)
                if event.get("ts", 0) < to_ts:
                    best_offset = current_offset
            except (json.JSONDecodeError, ValueError):
                pass
        current_offset += len(line.encode("utf-8"))

    save_cursor(channel, agent, best_offset)
