#!/usr/bin/env python3
"""
Ghost Daemon — standalone scheduler for autonomous agent workflows.

No Telegram, no external dependencies. Workflows are auto-discovered
Python modules. Communication happens via append-only JSONL channels.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from .config import (
    load_config,
    get_llm_config,
    RUNS_DIR,
    STATE_PATH,
)
from .workflows import get_workflow
from .scheduler import should_run, parse_interval

LOG_FILE = RUNS_DIR / "ghost.log"


def _setup_logging():
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
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
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception as e:
        logger.error(f"State load failed: {e}")
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        logger.error(f"State save failed: {e}")


class GhostDaemon:
    """Standalone daemon — schedules and dispatches workflow modules."""

    def __init__(self):
        self.config = load_config()
        self.state = load_state()
        self.running = False

        llm_config = get_llm_config()
        self.llm_client = None
        self.model = llm_config.get("model")
        if llm_config.get("api_key"):
            try:
                from openai import AsyncOpenAI
                self.llm_client = AsyncOpenAI(
                    api_key=llm_config["api_key"],
                    base_url=llm_config["base_url"],
                )
            except ImportError:
                logger.info("openai package not installed — LLM features unavailable")

    async def start(self):
        self.running = True
        logger.info("ghost daemon starting")
        logger.info(f"Loaded {len(self.config.get('jobs', []))} jobs")

        self._suppress_missed_jobs()

        try:
            await self._run_loop()
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self.running = False
        logger.info("ghost daemon stopping")

    def _suppress_missed_jobs(self):
        """Advance last_run to now for any overdue jobs on startup."""
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
            logger.info(f"Suppressed {len(suppressed)} missed jobs on startup")

    def _consume_events(self) -> list[str]:
        fresh = load_state()
        events = fresh.pop("events", [])
        if events:
            save_state(fresh)
        return events

    async def _run_loop(self):
        while self.running:
            try:
                self.config = load_config()
                self.state = load_state()
                now = datetime.now()

                # Dispatch event-triggered jobs
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

                # Dispatch manual triggers
                triggers = self.state.get("triggers", [])
                if triggers:
                    jobs_by_name = {j["name"]: j for j in self.config.get("jobs", [])}
                    for trigger_name in triggers:
                        if trigger_name in jobs_by_name:
                            logger.info(f"Manual trigger: {trigger_name}")
                            asyncio.create_task(self._run_job(jobs_by_name[trigger_name], manual=True))
                    fresh = load_state()
                    fresh.pop("triggers", None)
                    save_state(fresh)
                    self.state = fresh

                # Check scheduled jobs
                for job in self.config.get("jobs", []):
                    if not job.get("enabled", True):
                        continue

                    name = job.get("name", "unknown")

                    last_run_str = self.state.get("last_run", {}).get(name)
                    last_run = None
                    if last_run_str:
                        try:
                            last_run = datetime.fromisoformat(last_run_str)
                        except Exception:
                            pass

                    not_before_str = self.state.get("not_before", {}).get(name)
                    if not_before_str:
                        try:
                            not_before_dt = datetime.fromisoformat(not_before_str)
                            now_cmp = now.astimezone() if not_before_dt.tzinfo else now
                            if now_cmp < not_before_dt:
                                continue
                        except Exception:
                            pass

                    match = should_run(job, now, last_run)
                    if match.should_run:
                        logger.info(f"Spawned: {name}")
                        asyncio.create_task(self._run_job(job))

                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                await asyncio.sleep(10)

    async def _run_job(self, job: dict, manual: bool = False, trigger: str | None = None):
        name = job.get("name", "unknown")
        workflow = job.get("workflow", name)

        if not manual:
            fresh_state = load_state()
            if "last_run" not in fresh_state:
                fresh_state["last_run"] = {}
            schedule = job.get("schedule", "")
            if isinstance(schedule, list):
                schedule = next((s for s in schedule if s.startswith("every ")), "")
            if schedule.startswith("every "):
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

        job_module = get_workflow(workflow)
        if not job_module:
            logger.warning(f"Unknown workflow: {workflow}")
            return

        try:
            config = {"model": self.model, "manual": manual}
            if trigger:
                config["trigger"] = trigger
            if job.get("config"):
                config.update(job["config"])

            if not manual and hasattr(job_module, "should_run_check"):
                if not job_module.should_run_check():
                    return

            if hasattr(job_module, "run"):
                await job_module.run(self.llm_client, config)
                logger.info(f"Job {name} finished")
            else:
                logger.warning(f"Workflow {workflow} has no run() function")

        except Exception as e:
            logger.error(f"Job {name} failed: {e}")


async def main():
    _setup_logging()
    daemon = GhostDaemon()

    loop = asyncio.get_event_loop()

    def handle_signal():
        asyncio.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    await daemon.start()


if __name__ == "__main__":
    asyncio.run(main())
