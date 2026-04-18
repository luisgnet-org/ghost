#!/usr/bin/env python3
"""Agent-facing message CLI. Wraps ghost.channels."""

import argparse
import os
import sys
import time
from pathlib import Path

ghost_home = Path(os.environ.get("GHOST_HOME", Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(ghost_home))

from ghost.channels import write, read, poll


def _agent_name():
    return os.environ.get("GHOST_AGENT_NAME", "agent")


def cmd_send(args):
    agent = _agent_name()
    write(agent, args.message, from_id=agent, source="agent")
    print(f"sent → {agent}")


def cmd_wait(args):
    agent = _agent_name()
    timeout = args.timeout
    start = time.time()
    print(f"waiting for messages (timeout={timeout}s)...")

    while time.time() - start < timeout:
        messages, cursor = poll(agent, timeout=5)
        for msg in messages:
            if msg.get("from") != agent:
                print(f"\n[{msg.get('from', '?')}] {msg.get('text', '')}")
        if not messages:
            time.sleep(2)

    print("wait timed out")


def cmd_check(args):
    agent = _agent_name()
    messages, _ = read(agent)
    recent = messages[-5:] if len(messages) > 5 else messages
    if not recent:
        print("no messages")
        return
    for msg in recent:
        print(f"[{msg.get('from', '?')}] {msg.get('text', '')}")


def main():
    parser = argparse.ArgumentParser(prog="messages")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("send")
    p.add_argument("message", type=str)

    p = sub.add_parser("wait")
    p.add_argument("--timeout", type=int, default=3600)

    sub.add_parser("check")

    args = parser.parse_args()

    commands = {
        "send": cmd_send,
        "wait": cmd_wait,
        "check": cmd_check,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
