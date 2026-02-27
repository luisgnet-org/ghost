# ghost

An autonomous daemon that runs scheduled workflows with Telegram integration and LLM-powered agents.

## What it does

Ghost is a background daemon that:
- Runs jobs on configurable schedules (intervals, cron-like, event-driven)
- Integrates with Telegram for notifications, commands, and interactive menus
- Hot-reloads configuration and auto-discovers workflow modules
- Manages shared state across workflows

## How to use this

Ghost is the engine. On its own it doesn't do much — it just runs workflows
on schedule and talks to Telegram. The real value comes from the workflows
you plug into it.

The reference implementation is **ghost-claw**, a personality plugin that
turns ghost into an autonomous AI teammate: it shuttles Telegram messages
into an inbox, launches Claude Code sessions inside a macOS sandbox, and
gives the agent persistent memory, identity files, and a council debate
framework. But you can build any kind of agent or automation on top of ghost.

To get started:
1. Set up the daemon (below)
2. Write a workflow or install a plugin (drop files in `ghost/workflows/`)
3. Configure schedules in `config/config.yaml`
4. The daemon handles the rest — hot-reloading, state management, retries

## Quick start

```bash
# Clone and setup
git clone <repo-url>
cd ghost
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure — create a .env file with your keys
cat > .env << 'ENVEOF'
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
# Optional — only needed by LLM-powered workflows
# OPENAI_API_KEY=your-key
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_MODEL=gpt-4
ENVEOF

# Start
ghost/bin/start.sh
```

## Configuration

Jobs are defined in `config/config.yaml` and hot-reloaded:

```yaml
jobs:
  - name: my_workflow
    schedule: "every 30m"      # or "daily 9:00", "weekdays 6:00", "on_wake"
    workflow: my_workflow       # maps to ghost/workflows/my_workflow.py
    run_while_sleeping: false   # skip when user is sleeping (optional)
    enabled: true
```

## Writing workflows

Drop a Python file in `ghost/workflows/`:

```python
# ghost/workflows/my_workflow.py

async def run(tg, llm_client, config):
    """Called by the daemon on schedule."""
    await tg.send_message("Hello from my workflow!", topic="general")
```

The daemon auto-discovers it. Register in `config/config.yaml` to schedule it.

## Architecture

```
daemon.py          Main async loop — checks schedules, dispatches jobs
scheduler.py       Schedule parsing (intervals, daily, weekdays, events)
config.py          Paths, env vars, shared state management
telegram/          Full Telegram client (send, wait, topics, reactions, menus)
workflows/         Auto-discovered job modules
services/          Shared services (topic icons, utilities)
```

## Telegram features

- Forum topic support (create, resolve, send to specific topics)
- Inline keyboards and callback handling
- Event waiting with filters (reply, thread, topic, callback data)
- MarkdownV2 auto-escaping
- Bot command registration
- SQLite-backed event store with pruning

## License

MIT
