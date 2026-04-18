"""
AgentRuntime — spawn and manage opencode agent sessions.

Usage from daemon workflows:

    runtime = AgentRuntime(config)
    agent_id = await runtime.spawn(
        task_id=42,
        agent_name="proj-a",
        model="vllm/qwen2.5-coder-32b",
    )
    active = runtime.list_active()
    runtime.kill(agent_id)
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ghost.config import GHOST_HOME, AGENTS_DIR, RUNS_DIR

logger = logging.getLogger("ghost")

SESSIONS_DIR = RUNS_DIR / "sessions"
PIDS_DIR = RUNS_DIR / "pids"
TASK_BOARD = GHOST_HOME / ".tasks.json"
TASK_LOG = GHOST_HOME / ".tasks.jsonl"


class AgentRuntime:
    """Spawn and manage opencode agent sessions."""

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in (SESSIONS_DIR, PIDS_DIR, AGENTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    async def spawn(
        self,
        task_id: int,
        agent_name: str = "default",
        model: str | dict | None = None,
        prompt: str | None = None,
        timeout: int = 1800,
    ) -> str:
        """Spawn an opencode agent for a task. Returns agent_id.

        model can be a string (model name) or a dict with full config:
            {"provider": "groq", "model": "llama-3.3-70b", "api_key": "...", "base_url": "..."}
        """
        agent_id = f"agent_{agent_name}_{task_id}_{datetime.now().strftime('%H%M%S')}"
        session_dir = self._create_session_dir(agent_id)
        workspace = self._prepare_workspace(agent_name, task_id)

        if prompt is None:
            prompt = self._build_prompt(task_id, agent_name)

        self._write_opencode_config(workspace, model)
        env = self._build_env(agent_id, agent_name, session_dir)

        log_path = session_dir / f"{agent_id}.log"
        stderr_path = session_dir / f"{agent_id}.stderr"

        cmd = ["opencode", "run", prompt]

        stdout_fh = open(log_path, "wb")
        stderr_fh = open(stderr_path, "wb")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_fh,
                stderr=stderr_fh,
                cwd=str(workspace),
                env=env,
                start_new_session=True,
            )
        except Exception:
            stdout_fh.close()
            stderr_fh.close()
            raise

        pid_file = PIDS_DIR / f"{agent_id}.json"
        pid_file.write_text(json.dumps({
            "agent_id": agent_id,
            "pid": proc.pid,
            "task_id": task_id,
            "agent_name": agent_name,
            "model": model,
            "timeout": timeout,
            "workspace": str(workspace),
            "session_dir": str(session_dir),
            "started_at": datetime.now().isoformat(),
        }, indent=2))

        logger.info(
            f"agent spawned: {agent_id} (pid={proc.pid}, task={task_id}, "
            f"agent={agent_name})"
        )
        return agent_id

    def kill(self, agent_id: str) -> bool:
        """Kill a running agent. Returns True if it was alive."""
        pid_file = PIDS_DIR / f"{agent_id}.json"
        if not pid_file.exists():
            return False

        info = json.loads(pid_file.read_text())
        pid = info["pid"]

        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        pid_file.unlink(missing_ok=True)
        logger.info(f"agent killed: {agent_id} (pid={pid})")
        return True

    def list_active(self) -> list[dict]:
        """Return info dicts for all live agents. Cleans up dead ones."""
        active = []
        for pid_file in PIDS_DIR.glob("*.json"):
            try:
                info = json.loads(pid_file.read_text())
            except (json.JSONDecodeError, OSError):
                pid_file.unlink(missing_ok=True)
                continue

            pid = info.get("pid")
            try:
                os.kill(pid, 0)
                active.append(info)
            except (ProcessLookupError, TypeError):
                pid_file.unlink(missing_ok=True)
                agent_id = info.get("agent_id")
                logger.info(f"agent cleaned up (dead): {agent_id} (pid={pid})")

        return active

    def cleanup(self) -> list[str]:
        """Kill agents that have exceeded their timeout."""
        killed = []
        now = datetime.now()
        for info in self.list_active():
            timeout = info.get("timeout", 1800)
            if timeout is None:
                continue
            started = datetime.fromisoformat(info["started_at"])
            elapsed = (now - started).total_seconds()
            if elapsed > timeout:
                self.kill(info["agent_id"])
                killed.append(info["agent_id"])
                logger.info(
                    f"agent timed out: {info['agent_id']} "
                    f"({elapsed:.0f}s > {timeout}s)"
                )
        return killed

    def _create_session_dir(self, agent_id: str) -> Path:
        now = datetime.now()
        session_dir = SESSIONS_DIR / now.strftime("%Y/%m/%d") / agent_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _prepare_workspace(self, agent_name: str, task_id: int) -> Path:
        """Set up the agent's workspace directory."""
        workspace = AGENTS_DIR / agent_name
        workspace.mkdir(parents=True, exist_ok=True)

        # Symlink shared task board
        for name, target in [(".tasks.json", TASK_BOARD), (".tasks.jsonl", TASK_LOG)]:
            link = workspace / name
            if link.is_symlink():
                if link.resolve() != target.resolve():
                    link.unlink()
                    link.symlink_to(target)
            elif not link.exists():
                if target.exists():
                    link.symlink_to(target)

        return workspace

    def _write_opencode_config(self, workspace: Path, model: str | dict | None) -> None:
        """Write opencode.json into the workspace with the model config."""
        config = {"$schema": "https://opencode.ai/config.json"}

        if model is None:
            config["model"] = "opencode/default"
        elif isinstance(model, str):
            config["model"] = model
        elif isinstance(model, dict):
            provider = model.get("provider", "openai")
            model_name = model.get("model", "default")
            config["model"] = f"{provider}/{model_name}"

            if model.get("api_key"):
                api_key = os.path.expandvars(model["api_key"])
                config.setdefault("providers", {})[provider] = {"apiKey": api_key}
            if model.get("base_url"):
                config.setdefault("providers", {})[provider] = {
                    **config.get("providers", {}).get(provider, {}),
                    "baseURL": model["base_url"],
                }

        (workspace / "opencode.json").write_text(json.dumps(config, indent=2))

    def _build_env(self, agent_id: str, agent_name: str, session_dir: Path) -> dict:
        env = os.environ.copy()
        env["GHOST_AGENT_ID"] = agent_id
        env["GHOST_AGENT_NAME"] = agent_name
        env["GHOST_SESSION_DIR"] = str(session_dir)
        env["GHOST_HOME"] = str(GHOST_HOME)
        return env

    def _build_prompt(self, task_id: int, agent_name: str) -> str:
        return (
            f"You are a research agent. Your task ID is {task_id}.\n"
            f"Agent: {agent_name}.\n\n"
            "## Boot\n\n"
            "1. Read AGENT.md for your instructions and identity.\n"
            "2. If SOUL/identity.md exists, read it.\n"
            f"3. Claim your task: `python3 bin/tasks.py claim {task_id}`\n"
            f"4. Read the task: `python3 bin/tasks.py show {task_id}`\n"
            "5. Do the work.\n"
            f"6. Deliver: `python3 bin/tasks.py deliver {task_id} \"<result>\"`\n"
            "7. Exit.\n\n"
            "## Rules\n\n"
            f"- Log progress: `python3 bin/tasks.py progress {task_id} \"<update>\"`\n"
            f"- If blocked: `python3 bin/tasks.py msg {task_id} \"blocked: <reason>\"`\n"
            "- Stay focused on your single task.\n"
        )
