#!/usr/bin/env python3
"""
Ghost Agency Daemon.

Runs scheduled jobs using the agency module for agentic execution.
Keeps the scheduler from original ghost but replaces workflow execution
with agency agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

from agency import Agent, AgentCallbacks, run_agent
from agency.plugins.session_log import (
    finalize_session,
    with_session_logging,
    with_tool_logging,
)
from ghost.telegram import TelegramClient

# Local imports
from .config import (
    load_config,
    get_llm_config,
    get_telegram_config,
    PROJECT_ROOT,
    RUNS_DIR,
    STATE_PATH,
    TELEGRAM_DB_PATH,
    workflow_dir,
)
from .workflows import get_workflow

# Use scheduler from original ghost
from ghost.scheduler import should_run

# Logging
LOG_FILE = RUNS_DIR / "ghost.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ghost")


def load_state() -> dict:
    """Load state from JSON."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception as e:
        logger.error(f"State load failed: {e}")
    return {}


def save_state(state: dict) -> None:
    """Save state to JSON."""
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        logger.error(f"State save failed: {e}")


class GhostAgencyDaemon:
    """Main daemon class using agency for job execution."""

    def __init__(self):
        self.config = load_config()
        self.state = load_state()
        self.running = False

        # LLM client
        llm_config = get_llm_config()
        if llm_config["api_key"]:
            self.llm_client = AsyncOpenAI(
                api_key=llm_config["api_key"],
                base_url=llm_config["base_url"],
            )
            self.model = llm_config["model"]
        else:
            self.llm_client = None
            self.model = None
            logger.warning("No LLM_API_KEY set, agent jobs will be skipped")

        # Telegram client
        tg_config = get_telegram_config()
        if tg_config["bot_token"] and tg_config["chat_id"]:
            self.tg = TelegramClient(
                bot_token=tg_config["bot_token"],
                chat_id=int(tg_config["chat_id"]),
                db_path=str(TELEGRAM_DB_PATH),
                poll_interval=2.0,
            )
        else:
            self.tg = None
            logger.warning("Telegram not configured")

        # Telegram command cursor — tracks last processed update_id
        self._tg_cmd_cursor = None
        self._bot_user_id = None

    async def start(self):
        """Start the daemon."""
        self.running = True
        self._mcp_server = None
        logger.info("ghost starting")
        logger.info(f"Loaded {len(self.config.get('jobs', []))} jobs")

        # Start Telegram client
        if self.tg:
            await self.tg.start()
            # Register /trigger_<name> commands for Telegram autocomplete
            job_names = [j["name"] for j in self.config.get("jobs", []) if j.get("enabled", True) and j.get("register_command", True)]
            await self.tg.register_commands(job_names)
            logger.info("TelegramClient started")

        # Start MCP server (daemon-lifetime, fixed port)
        if self.tg:
            try:
                from .services.mcp import AgentMCPServer
                self._mcp_server = AgentMCPServer(self.tg)
                ok = await self._mcp_server.start()
                if not ok:
                    logger.error("MCP server failed to start")
                    self._mcp_server = None
            except Exception as e:
                logger.error(f"MCP server init failed: {e}")
                self._mcp_server = None

        # Advance last_run to now for all enabled jobs so we never "catch up"
        # on missed schedules after a restart. Jobs will fire at their next
        # natural cadence instead of all firing immediately.
        self._suppress_missed_jobs()

        # Main loop
        try:
            await self._run_loop()
        finally:
            if self._mcp_server:
                await self._mcp_server.stop()
            if self.tg:
                await self.tg.stop()

    async def stop(self):
        """Stop the daemon."""
        self.running = False
        logger.info("ghost stopping")

    def _is_sleeping(self) -> bool:
        """Check if user is currently sleeping via shared state (set by toggl_sync)."""
        from .config import get_shared
        return get_shared("is_sleeping", False)

    def _suppress_missed_jobs(self):
        """Advance last_run to now for any overdue jobs.

        Prevents the daemon from firing all "missed" jobs on restart.
        Without this, a crash loop (e.g. disk full) causes every restart
        to re-send all overdue messages since last_run can't be persisted.
        """
        now = datetime.now()
        fresh = load_state()
        last_run_map = fresh.get("last_run", {})
        suppressed = []

        for job in self.config.get("jobs", []):
            if not job.get("enabled", True):
                continue
            name = job.get("name", "unknown")
            last_run_str = last_run_map.get(name)
            if not last_run_str:
                # Never ran — let it fire on its first natural schedule
                last_run_map[name] = now.isoformat()
                suppressed.append(name)
                continue

            try:
                last_run = datetime.fromisoformat(last_run_str)
            except Exception:
                continue

            match = should_run(job, now, last_run)
            if match.should_run:
                last_run_map[name] = now.isoformat()
                suppressed.append(name)

        if suppressed:
            fresh["last_run"] = last_run_map
            save_state(fresh)
            self.state = fresh
            logger.info(f"Suppressed {len(suppressed)} missed jobs on startup: {', '.join(suppressed)}")

    def _consume_events(self) -> list[str]:
        """Consume pending events from state.json. Returns list of event names."""
        fresh = load_state()
        events = fresh.pop("events", [])
        if events:
            save_state(fresh)
        return events

    async def _check_telegram_commands(self):
        """Poll for /trigger_* commands across all topics."""
        if not self.tg:
            return

        store = self.tg.store

        # Seed cursor on first call
        if self._tg_cmd_cursor is None:
            all_recent = await store.query_events(since_update_id=0, limit=1000)
            self._tg_cmd_cursor = all_recent[-1]["update_id"] if all_recent else 0

        if self._bot_user_id is None:
            bot_info = await self.tg.bot.get_me()
            self._bot_user_id = bot_info.id

        # Query all new messages (no topic filter)
        events = await store.query_events(
            event_type="message",
            since_update_id=self._tg_cmd_cursor,
            limit=20,
        )

        jobs_by_name = {j["name"]: j for j in self.config.get("jobs", [])}

        for event in events:
            self._tg_cmd_cursor = event["update_id"]

            if event.get("user_id") == self._bot_user_id:
                continue

            text = event.get("text", "")
            topic_id = event.get("topic_id")

            # /kill_session — emergency agent session termination
            if text.startswith("/kill_session"):
                try:
                    from .workflows import get_workflow
                    agent_wf = get_workflow("agent")
                    if agent_wf and hasattr(agent_wf, "kill_session"):
                        result = agent_wf.kill_session()
                    else:
                        result = "no active session"
                    await self.tg.send_message(f"agent: {result}", topic=topic_id)
                    logger.info(f"kill_session: {result}")
                except Exception as e:
                    await self.tg.send_message(f"kill_session failed: {e}", topic=topic_id)
                continue

            if not text.startswith("/trigger_"):
                continue

            job_name = text.split("@")[0][9:]  # strip /trigger_ prefix and @botname

            if job_name not in jobs_by_name:
                await self.tg.send_message(
                    f"Unknown job: `{job_name}`", topic=topic_id, parse_mode="Markdown",
                )
                continue

            try:
                fresh = load_state()
                triggers = fresh.get("triggers", [])
                triggers.append(job_name)
                fresh["triggers"] = triggers
                save_state(fresh)
                await self.tg.send_message(
                    f"Triggered `{job_name}`", topic=topic_id, parse_mode="Markdown",
                )
                logger.info(f"Telegram trigger: {job_name} (from topic {topic_id})")
            except Exception as e:
                await self.tg.send_message(
                    f"Failed to trigger {job_name}: {e}", topic=topic_id, parse_mode=None,
                )

    async def _run_loop(self):
        """Main scheduling loop."""
        while self.running:
            try:
                # Reload config + state each iteration (hot reload)
                self.config = load_config()
                self.state = load_state()
                now = datetime.now()
                sleeping = self._is_sleeping()

                # Check Telegram /trigger_* commands (all topics)
                await self._check_telegram_commands()

                # Consume events emitted by workflows and dispatch matching schedules
                for event_name in self._consume_events():
                    schedule_name = f"on_{event_name}"
                    for job in self.config.get("jobs", []):
                        if not job.get("enabled", True):
                            continue
                        sched = job.get("schedule", "")
                        schedules = sched if isinstance(sched, list) else [sched]
                        if schedule_name in schedules:
                            logger.info(f"{schedule_name}: spawning {job.get('name')}")
                            asyncio.create_task(self._run_job(job, trigger=event_name))

                # Check manual triggers (don't update last_run)
                triggers = self.state.get("triggers", [])
                if triggers:
                    jobs_by_name = {j["name"]: j for j in self.config.get("jobs", [])}
                    for trigger_name in triggers:
                        if trigger_name in jobs_by_name:
                            logger.info(f"Manual trigger: {trigger_name}")
                            asyncio.create_task(self._run_job(jobs_by_name[trigger_name], manual=True))
                    # Clear triggers
                    fresh = load_state()
                    fresh.pop("triggers", None)
                    save_state(fresh)
                    self.state = fresh

                # Check each job
                for job in self.config.get("jobs", []):
                    if not job.get("enabled", True):
                        continue

                    name = job.get("name", "unknown")

                    # Skip jobs that shouldn't run while sleeping
                    if sleeping and not job.get("run_while_sleeping", True):
                        continue

                    last_run_str = self.state.get("last_run", {}).get(name)
                    last_run = None
                    if last_run_str:
                        try:
                            last_run = datetime.fromisoformat(last_run_str)
                        except Exception:
                            pass

                    # Check not_before suppression
                    not_before_str = self.state.get("not_before", {}).get(name)
                    if not_before_str:
                        try:
                            not_before_dt = datetime.fromisoformat(not_before_str)
                            # Make now tz-aware if not_before_dt has tzinfo
                            now_cmp = now.astimezone() if not_before_dt.tzinfo else now
                            if now_cmp < not_before_dt:
                                continue
                        except Exception:
                            pass

                    # Check if should run
                    match = should_run(job, now, last_run)
                    if match.should_run:
                        logger.info(f"Spawned: {name}")
                        asyncio.create_task(self._run_job(job))

                # Sleep before next check (5s to support fast-polling jobs)
                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                await asyncio.sleep(10)

    async def _run_job(self, job: dict, manual: bool = False, trigger: str | None = None):
        """Execute a job using agency."""
        name = job.get("name", "unknown")
        workflow = job.get("workflow", name)

        # Update last_run immediately to prevent re-triggering
        # Skip for manual triggers — they shouldn't affect the schedule.
        if not manual:
            fresh_state = load_state()
            if "last_run" not in fresh_state:
                fresh_state["last_run"] = {}
            # For interval jobs, align last_run to clean boundaries so runs
            # stay on schedule (e.g. :00, :05, :10) instead of drifting.
            # < 1h: round down to nearest interval multiple. >= 1h: top of hour.
            schedule = job.get("schedule", "")
            # For list schedules, find the interval part (if any)
            if isinstance(schedule, list):
                schedule = next((s for s in schedule if s.startswith("every ")), "")
            if schedule.startswith("every "):
                from .scheduler import parse_interval
                now = datetime.now()
                interval = parse_interval(schedule[6:].strip())
                if interval.total_seconds() < 3600:
                    epoch = now.timestamp()
                    aligned = datetime.fromtimestamp(epoch - (epoch % interval.total_seconds()))
                else:
                    aligned = now.replace(minute=0, second=0, microsecond=0)
                fresh_state["last_run"][name] = aligned.isoformat()
            else:
                fresh_state["last_run"][name] = datetime.now().isoformat()
            save_state(fresh_state)
            self.state = fresh_state

        # Simple jobs that don't need an agent
        if workflow == "heartbeat":
            await self._run_heartbeat(job)
            return

        # Get job module
        job_module = get_workflow(workflow)
        if not job_module:
            logger.warning(f"Unknown job: {workflow}")
            return

        try:
            config = {"model": self.model}

            # Pre-check: skip if job says not to run (bypass for manual triggers)
            if not manual and hasattr(job_module, "should_run_check"):
                if not job_module.should_run_check():
                    return

            # Jobs with async run() handle their own agent lifecycle
            if hasattr(job_module, "run"):
                config["manual"] = manual
                if trigger:
                    config["trigger"] = trigger
                await job_module.run(self.tg, self.llm_client, config)
                logger.info(f"Job {name} finished")
                return

            # Check if we have LLM client (only needed for create_agent jobs)
            if not self.llm_client:
                logger.warning(f"Skipping {name}: no LLM client")
                return

            # Standard jobs: create_agent + run_agent
            state, callbacks = job_module.create_agent(self.tg, config)
            state.client = self.llm_client

            # Session logging — workflows/<name>/agency_sessions/YYYY-MM-DD_HHMMSS/
            now_ts = datetime.now()
            sd = workflow_dir(name) / "agency_sessions" / now_ts.strftime(f"%Y-%m-%d_%H%M%S")
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "calls").mkdir(exist_ok=True)
            state.callbacks = AgentCallbacks(
                llm_call=with_session_logging(sd)(callbacks.llm_call),
                execute_tool=with_tool_logging(sd)(callbacks.execute_tool),
                should_continue=callbacks.should_continue,
            )

            # Run agent
            result = await run_agent(state)

            # Finalize session log
            exit_reason = result.exit_reason or "completed"
            finalize_session(sd, status=exit_reason)
            logger.info(f"Job {name} finished: {exit_reason}")

        except Exception as e:
            logger.error(f"Job {name} failed: {e}")

    async def _run_heartbeat(self, job: dict):
        """Simple heartbeat - no agent needed."""
        if self.tg:
            message = job.get("message", "ghost is alive")
            await self.tg.send_message(message)
            logger.info(f"Heartbeat sent: {message}")


async def main():
    """Entry point."""
    daemon = GhostAgencyDaemon()

    # Handle signals
    loop = asyncio.get_event_loop()

    def handle_signal():
        asyncio.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    await daemon.start()


if __name__ == "__main__":
    asyncio.run(main())
