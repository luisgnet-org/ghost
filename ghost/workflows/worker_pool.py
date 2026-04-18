"""worker_pool — Dispatch agents from the task board.

Runs every 5s. Checks for tasks tagged 'needs-dispatch', spawns
opencode agents via AgentRuntime, enforces pool limits and timeouts.
"""

import json
import logging
import os
import sys
from pathlib import Path

from ghost.config import GHOST_HOME, load_config
from ghost.agent_runtime import AgentRuntime

sys.path.insert(0, str(GHOST_HOME))
from lib.tasks_core import set_task_state

logger = logging.getLogger("ghost")

TASK_BOARD = GHOST_HOME / ".tasks.json"

# Pool limits per agent
MAX_AGENTS = 3


def _load_board() -> dict:
    if TASK_BOARD.exists():
        try:
            return json.loads(TASK_BOARD.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"contracts": [], "nid": 1}


async def run(llm_client, config: dict):
    runtime = AgentRuntime(config)

    # Cleanup timed-out agents
    killed = runtime.cleanup()
    if killed:
        logger.info(f"worker_pool: cleaned up {len(killed)} timed-out agents")

    active = runtime.list_active()
    if len(active) >= MAX_AGENTS:
        return

    # Find tasks needing dispatch
    board = _load_board()
    for contract in board.get("contracts", []):
        if contract.get("status") != "open":
            continue
        meta = contract.get("meta", {})
        if meta.get("tag") != "needs-dispatch":
            continue
        if meta.get("dispatched"):
            continue

        if len(active) >= MAX_AGENTS:
            break

        task_id = contract["id"]
        agent_name = meta.get("agent", "worker")

        # Resolve model name to full config from models section
        model_name = config.get("model")
        full_config = load_config()
        models = full_config.get("models", {})
        model = models.get(model_name, model_name) if model_name else None

        try:
            agent_id = await runtime.spawn(
                task_id=task_id,
                agent_name=agent_name,
                model=model,
                timeout=contract.get("ttl", 1800),
            )

            # Transition task to claimed
            old_agent = os.environ.get("GHOST_AGENT_ID")
            os.environ["GHOST_AGENT_ID"] = agent_id
            try:
                set_task_state(task_id, "claimed")
            finally:
                if old_agent:
                    os.environ["GHOST_AGENT_ID"] = old_agent
                else:
                    os.environ.pop("GHOST_AGENT_ID", None)

            active = runtime.list_active()
            logger.info(f"worker_pool: dispatched task #{task_id} → {agent_id}")

        except Exception as e:
            logger.error(f"worker_pool: failed to spawn for task #{task_id}: {e}")
