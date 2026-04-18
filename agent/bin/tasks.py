#!/usr/bin/env python3
"""Agent-facing task CLI. Wraps lib/tasks_core.py."""

import argparse
import json
import os
import sys
from pathlib import Path

ghost_home = Path(os.environ.get("GHOST_HOME", Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(ghost_home))

from lib.tasks_core import (
    create_task,
    set_task_state,
    get_task_state,
    _load,
    _get_agent_id,
)


def cmd_current(args):
    data = _load()
    agent = _get_agent_id()
    found = []
    for c in data["contracts"]:
        if c.get("claimed_by") == agent and c["status"] not in ("delivered", "cancelled", "failed"):
            found.append(c)
    if not found:
        print("No active tasks.")
        return
    for c in found:
        print(f"  #{c['id']} [{c['status']}] {c['title']}")


def cmd_show(args):
    state = get_task_state(args.id)
    print(json.dumps(state, indent=2))


def cmd_claim(args):
    result = set_task_state(args.id, "claimed")
    print(f"Claimed task #{args.id}")


def cmd_deliver(args):
    set_task_state(args.id, "in_progress")
    result = set_task_state(args.id, "delivered", result=args.result)
    print(f"Delivered task #{args.id}")


def cmd_progress(args):
    data = _load()
    for c in data["contracts"]:
        if c["id"] == args.id:
            c.setdefault("progress", []).append({
                "ts": __import__("datetime").datetime.now().isoformat(),
                "text": args.message,
                "agent": _get_agent_id(),
            })
            c["updated_at"] = __import__("datetime").datetime.now().isoformat()
            Path(os.environ.get("GHOST_HOME", ".")).joinpath(".tasks.json").write_text(
                json.dumps(data, indent=2)
            )
            print(f"Progress logged for task #{args.id}")
            return
    print(f"Task #{args.id} not found")


def cmd_list(args):
    data = _load()
    for c in data["contracts"]:
        if args.active and c["status"] in ("delivered", "cancelled", "failed"):
            continue
        print(f"  #{c['id']} [{c['status']}] {c['title']}")


def cmd_msg(args):
    data = _load()
    for c in data["contracts"]:
        if c["id"] == args.id:
            c.setdefault("messages", []).append({
                "ts": __import__("datetime").datetime.now().isoformat(),
                "text": args.message,
                "agent": _get_agent_id(),
            })
            Path(os.environ.get("GHOST_HOME", ".")).joinpath(".tasks.json").write_text(
                json.dumps(data, indent=2)
            )
            print(f"Message sent on task #{args.id}")
            return
    print(f"Task #{args.id} not found")


def main():
    parser = argparse.ArgumentParser(prog="tasks")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("current")

    p = sub.add_parser("show")
    p.add_argument("id", type=int)

    p = sub.add_parser("claim")
    p.add_argument("id", type=int)

    p = sub.add_parser("deliver")
    p.add_argument("id", type=int)
    p.add_argument("result", type=str)

    p = sub.add_parser("progress")
    p.add_argument("id", type=int)
    p.add_argument("message", type=str)

    p = sub.add_parser("list")
    p.add_argument("--active", action="store_true")

    p = sub.add_parser("msg")
    p.add_argument("id", type=int)
    p.add_argument("message", type=str)

    args = parser.parse_args()

    commands = {
        "current": cmd_current,
        "show": cmd_show,
        "claim": cmd_claim,
        "deliver": cmd_deliver,
        "progress": cmd_progress,
        "list": cmd_list,
        "msg": cmd_msg,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
