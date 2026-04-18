"""research_loop — Design → fan-out → collect → postmortem → repeat.

Configurable research workflow. Each cycle:
1. Design: LLM generates research questions from a goal
2. Fan-out: Creates tasks for each question (worker_pool dispatches agents)
3. Collect: Waits for all tasks to complete
4. Postmortem: LLM synthesizes results, decides next iteration
5. Repeat or conclude

Config (in config.yaml job config):
  goal: "What are the most promising approaches to X?"
  max_cycles: 3
  fan_out: 3          # questions per cycle
  agent: "researcher" # agent template name
  timeout: 1800       # per-task timeout
"""

import json
import logging
import sys
import time
from pathlib import Path

from ghost.config import GHOST_HOME, get_shared, set_shared, workflow_dir

sys.path.insert(0, str(GHOST_HOME))
from lib.tasks_core import create_task, get_task_state, _load

logger = logging.getLogger("ghost")

COLLECT_POLL_INTERVAL = 30
COLLECT_TIMEOUT = 7200


def _state_key(job_name: str) -> str:
    return f"research_loop_{job_name}"


async def _design_questions(llm_client, goal: str, prior_results: list[str], fan_out: int) -> list[str]:
    if not llm_client:
        return [f"Research: {goal}"]

    prior_context = ""
    if prior_results:
        prior_context = "\n\nPrior cycle results:\n" + "\n".join(f"- {r}" for r in prior_results)

    response = await llm_client.chat.completions.create(
        model="default",
        messages=[{
            "role": "user",
            "content": (
                f"You are a research director. Given this goal:\n\n{goal}\n"
                f"{prior_context}\n\n"
                f"Generate exactly {fan_out} specific, actionable research questions. "
                f"Each should be investigatable by a single agent in under 30 minutes. "
                f"Return as a JSON array of strings, nothing else."
            ),
        }],
        temperature=0.7,
    )

    text = response.choices[0].message.content.strip()
    try:
        questions = json.loads(text)
        if isinstance(questions, list):
            return questions[:fan_out]
    except json.JSONDecodeError:
        pass
    return [f"Research: {goal}"]


async def _postmortem(llm_client, goal: str, results: list[dict], cycle: int) -> dict:
    if not llm_client:
        return {"continue": False, "summary": "No LLM available for synthesis"}

    results_text = "\n\n".join(
        f"Question: {r['question']}\nResult: {r['result'] or 'No result delivered'}"
        for r in results
    )

    response = await llm_client.chat.completions.create(
        model="default",
        messages=[{
            "role": "user",
            "content": (
                f"You are a research director reviewing cycle {cycle} results.\n\n"
                f"Goal: {goal}\n\n"
                f"Results:\n{results_text}\n\n"
                f"Respond with JSON only:\n"
                f'{{"continue": true/false, "summary": "what we learned", '
                f'"next_direction": "refined focus for next cycle if continuing"}}'
            ),
        }],
        temperature=0.3,
    )

    text = response.choices[0].message.content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"continue": False, "summary": text}


async def run(llm_client, config: dict):
    job_name = config.get("name", "research")
    goal = config.get("goal")
    if not goal:
        logger.warning("research_loop: no goal configured")
        return

    max_cycles = config.get("max_cycles", 3)
    fan_out = config.get("fan_out", 3)
    agent_name = config.get("agent", "researcher")
    timeout = config.get("timeout", 1800)

    state_key = _state_key(job_name)
    state = get_shared(state_key) or {"cycle": 0, "results": [], "status": "idle"}

    if state["status"] == "waiting":
        task_ids = state.get("task_ids", [])
        results = []
        all_done = True

        for tid in task_ids:
            try:
                ts = get_task_state(tid)
                if ts["status"] in ("delivered", "failed", "cancelled"):
                    results.append({
                        "question": ts.get("title", ""),
                        "result": ts.get("result"),
                        "status": ts["status"],
                    })
                else:
                    all_done = False
            except KeyError:
                results.append({"question": "?", "result": None, "status": "missing"})

        if not all_done:
            elapsed = time.time() - state.get("fan_out_at", 0)
            if elapsed < COLLECT_TIMEOUT:
                return
            logger.warning(f"research_loop: collect timeout after {elapsed:.0f}s")

        # Postmortem
        cycle = state["cycle"]
        review = await _postmortem(llm_client, goal, results, cycle)

        run_dir = workflow_dir(job_name)
        (run_dir / f"cycle_{cycle}.json").write_text(json.dumps({
            "cycle": cycle,
            "goal": goal,
            "results": results,
            "review": review,
        }, indent=2))

        logger.info(f"research_loop: cycle {cycle} complete — {review.get('summary', '')[:100]}")

        state["results"].append(review.get("summary", ""))

        if not review.get("continue", False) or cycle >= max_cycles:
            state["status"] = "done"
            set_shared(state_key, state)
            logger.info(f"research_loop: finished after {cycle} cycles")
            return

        if review.get("next_direction"):
            goal = review["next_direction"]

        state["cycle"] = cycle + 1
        state["status"] = "idle"
        set_shared(state_key, state)

    if state["status"] in ("idle", "done"):
        if state["status"] == "done":
            return

        cycle = state["cycle"] + 1
        prior = state.get("results", [])

        questions = await _design_questions(llm_client, goal, prior, fan_out)
        logger.info(f"research_loop: cycle {cycle} — {len(questions)} questions")

        task_ids = []
        for q in questions:
            tid = create_task(
                q,
                timeout=timeout,
                meta={"tag": "needs-dispatch", "agent": agent_name, "research_cycle": cycle},
            )
            task_ids.append(tid)

        state["cycle"] = cycle
        state["status"] = "waiting"
        state["task_ids"] = task_ids
        state["fan_out_at"] = time.time()
        set_shared(state_key, state)

        logger.info(f"research_loop: dispatched {len(task_ids)} tasks for cycle {cycle}")
