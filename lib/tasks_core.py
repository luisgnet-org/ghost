"""tasks_core.py — Task queue primitives for agent orchestration.

Append-only .tasks.json + .tasks.jsonl with flock-based concurrency.
"""

import asyncio
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

GHOST_HOME = Path(os.environ.get("GHOST_HOME", Path(__file__).resolve().parent.parent))
DB = GHOST_HOME / ".tasks.json"
LOG = GHOST_HOME / ".tasks.jsonl"
LOCKFILE = GHOST_HOME / ".tasks.lock"

DEFAULT_TTL = 600

VALID_TRANSITIONS = {
    "open":        {"claimed", "cancelled"},
    "claimed":     {"in_progress", "open", "cancelled"},
    "in_progress": {"delivered", "failed", "open", "cancelled"},
    "delivered":   set(),
    "cancelled":   set(),
    "failed":      {"open"},
}

TERMINAL_STATES = {"delivered", "cancelled", "failed"}


@contextmanager
def task_lock():
    with open(LOCKFILE, "a") as _lf:
        fcntl.flock(_lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(_lf, fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if DB.exists():
        return json.loads(DB.read_text())
    return {"contracts": [], "nid": 1}


def _save(data: dict) -> None:
    DB.write_text(json.dumps(data, indent=2))


def _append_event(event_type: str, contract_id: int, text: str, agent: str, extra: dict = None) -> None:
    event = {
        "ts": _now_iso(),
        "type": event_type,
        "contract_id": contract_id,
        "agent": agent,
        "text": text,
    }
    if extra:
        event.update(extra)
    with open(LOG, "a") as f:
        f.write(json.dumps(event) + "\n")


def _find_contract(data: dict, task_id: int) -> dict | None:
    for c in data["contracts"]:
        if c["id"] == task_id:
            return c
    return None


def _get_agent_id() -> str:
    return (
        os.environ.get("GHOST_AGENT_ID")
        or os.environ.get("GHOST_SESSION_ID")
        or "unknown"
    )


def create_task(prompt: str, timeout: int = 600, context: dict = None, meta: dict = None) -> int:
    """Create a task. Returns task_id."""
    agent = _get_agent_id()
    m = dict(meta or {})
    m["tag"] = "needs-dispatch"

    with task_lock():
        data = _load()
        task_id = data["nid"]
        c = {
            "id": task_id,
            "title": prompt[:120],
            "deliverable": prompt,
            "status": "open",
            "posted_by": agent,
            "claimed_by": None,
            "claimed_at": None,
            "parent_id": None,
            "ttl": timeout,
            "result": None,
            "progress": [],
            "messages": [],
            "artifacts": [],
            "meta": m,
            "context": context or {},
            "assignee": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        data["contracts"].append(c)
        data["nid"] += 1
        _save(data)
        _append_event("posted", task_id, prompt, agent)

    return task_id


def set_task_state(task_id: int, state: str, **fields) -> dict:
    """Transition a task to a new state."""
    agent = _get_agent_id()

    with task_lock():
        data = _load()
        c = _find_contract(data, task_id)
        if c is None:
            raise KeyError(f"Task {task_id} not found")

        current = c["status"]
        allowed = VALID_TRANSITIONS.get(current, set())
        if state not in allowed:
            raise ValueError(f"Invalid transition: {current} -> {state}")

        c["status"] = state
        c["updated_at"] = _now_iso()

        if state == "claimed":
            c["claimed_by"] = agent
            c["claimed_at"] = _now_iso()
        elif state == "open":
            c["claimed_by"] = None
            c["claimed_at"] = None

        if "result" in fields:
            c["result"] = fields["result"]
        if "error" in fields:
            c.setdefault("meta", {})["error"] = fields["error"]

        _save(data)
        _append_event(state, task_id, f"{current} -> {state}", agent)
        return dict(c)


def get_task_state(task_id: int) -> dict:
    """Read current task state."""
    data = _load()
    c = _find_contract(data, task_id)
    if c is None:
        raise KeyError(f"Task {task_id} not found")

    elapsed = None
    try:
        created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, AttributeError):
        pass

    return {
        "id": c["id"],
        "status": c["status"],
        "result": c.get("result"),
        "elapsed": elapsed,
        "worker": c.get("claimed_by"),
        "title": c.get("title"),
        "deliverable": c.get("deliverable"),
        "meta": c.get("meta", {}),
        "context": c.get("context", {}),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }


async def await_task(task_id: int, timeout: int = 600, poll_interval: float = 3.0) -> dict:
    """Poll until task reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while True:
        state = get_task_state(task_id)
        if state["status"] in TERMINAL_STATES:
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Task {task_id} timed out after {timeout}s")
        await asyncio.sleep(poll_interval)
