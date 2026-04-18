"""worker_pool — Dispatch agents from the task board.

Runs every 5s. Checks for tasks tagged 'needs-dispatch', spawns
opencode agents via AgentRuntime, enforces pool limits and timeouts.
"""

import json
import logging
from pathlib import Path

from ghost.config import GHOST_HOME
from ghost.agent_runtime import AgentRuntime

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
        model = config.get("model")

        try:
            agent_id = await runtime.spawn(
                task_id=task_id,
                agent_name=agent_name,
                model=model,
                timeout=contract.get("ttl", 1800),
            )

            # Mark as dispatched
            meta["dispatched"] = True
            meta["agent_id"] = agent_id
            contract["meta"] = meta
            TASK_BOARD.write_text(json.dumps(board, indent=2))

            active = runtime.list_active()
            logger.info(f"worker_pool: dispatched task #{task_id} → {agent_id}")

        except Exception as e:
            logger.error(f"worker_pool: failed to spawn for task #{task_id}: {e}")
