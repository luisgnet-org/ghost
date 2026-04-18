"""daemon_status — periodic health check."""

import logging
from ghost.channels import write

logger = logging.getLogger("ghost")


async def run(llm_client, config: dict):
    write("daemon", "heartbeat", from_id="daemon", source="daemon", event_type="heartbeat")
    logger.info("daemon_status: heartbeat")
