#!/usr/bin/env python3
"""Send a message to an agent's channel.

Usage:
    python3 tui/send.py --agent AGENT_NAME "your message here"
    python3 tui/send.py --channel custom_channel "message"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghost.channels import write


def main():
    parser = argparse.ArgumentParser(description="Send a message to an agent channel")
    parser.add_argument("message", nargs="+", help="Message text")
    parser.add_argument("--agent", "-a", default="default", help="Agent name (channel = agent name)")
    parser.add_argument("--channel", "-c", help="Override channel name")
    parser.add_argument("--from", dest="from_id", default="user", help="Sender identity")
    args = parser.parse_args()

    channel = args.channel or args.agent
    text = " ".join(args.message)

    eid = write(channel, text, from_id=args.from_id, source="tui")
    if eid:
        print(f"sent → {channel} [{eid}]")
    else:
        print("failed to send", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
