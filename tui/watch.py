#!/usr/bin/env python3
"""Watch an agent's channel for new messages.

Usage:
    python3 tui/watch.py --agent AGENT_NAME
    python3 tui/watch.py --agent AGENT_NAME --follow
    python3 tui/watch.py --channel custom_channel --history 50
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghost.channels import read, path as channel_path, init_cursors


def format_event(event: dict) -> str:
    ts = event.get("ts", 0)
    dt = datetime.fromtimestamp(ts)
    time_str = dt.strftime("%H:%M:%S")
    sender = event.get("from", "?")
    source = event.get("source", "")
    text = event.get("text", "")
    event_type = event.get("type", "message")

    if event_type == "permission_request":
        return f"\033[33m[{time_str}] PERMISSION REQUEST from {sender}:\033[0m {text}"
    elif source == "tui":
        return f"\033[36m[{time_str}] {sender}:\033[0m {text}"
    elif source == "agent":
        return f"\033[32m[{time_str}] {sender}:\033[0m {text}"
    else:
        return f"[{time_str}] {sender} ({source}): {text}"


def show_history(channel: str, n: int = 20):
    """Show last N events from channel."""
    p = channel_path(channel)
    if not p.exists():
        print(f"No messages in channel '{channel}' yet.")
        return 0

    events = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    for event in events[-n:]:
        print(format_event(event))

    return p.stat().st_size


def follow(channel: str, offset: int):
    """Tail the channel for new messages."""
    print(f"\n--- watching {channel} (Ctrl+C to quit) ---\n")
    try:
        while True:
            events, new_offset = read(channel, offset)
            for event in events:
                if event.get("type") in ("message", "permission_request"):
                    print(format_event(event))
            offset = new_offset
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n--- stopped ---")


def main():
    parser = argparse.ArgumentParser(description="Watch an agent channel")
    parser.add_argument("--agent", "-a", default="default", help="Agent name")
    parser.add_argument("--channel", "-c", help="Override channel name")
    parser.add_argument("--follow", "-f", action="store_true", help="Continuously watch for new messages")
    parser.add_argument("--history", "-n", type=int, default=20, help="Number of historical messages to show")
    args = parser.parse_args()

    channel = args.channel or args.agent
    offset = show_history(channel, args.history)

    if args.follow:
        follow(channel, offset)


if __name__ == "__main__":
    main()
