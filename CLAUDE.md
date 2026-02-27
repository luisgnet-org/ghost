# ghost

Autonomous daemon with scheduled workflows, Telegram integration, and plugin-based extensibility.

## Structure

```
ghost/                              # repo root
├── ghost/                          # Python package
│   ├── daemon.py                   #   Main async daemon loop
│   ├── scheduler.py                #   Schedule parsing (cron, interval, event)
│   ├── config.py                   #   Configuration management
│   ├── telegram/                   #   Telegram client library
│   │   ├── client.py               #     Unified API (send, wait, topics)
│   │   ├── store.py                #     SQLite event storage
│   │   ├── _watcher.py             #     Long-poll update watcher
│   │   ├── wait.py                 #     Event waiting with filters
│   │   ├── markdown_v2.py          #     MarkdownV2 escaping
│   │   └── menus.py                #     Menu builder utilities
│   ├── workflows/                  #   Auto-discovered workflow modules
│   ├── services/                   #   Shared services (MCP, topic icons)
│   └── bin/                        #   Start/stop scripts
├── config/config.yaml              # Job definitions (hot-reloaded)
├── requirements.txt
├── .env.example
└── .gitignore
```

## Setup

```bash
# Create venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Start daemon
ghost/bin/start.sh
```

## Environment

Required in `.env`:
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` — LLM provider config
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Telegram bot + group chat

Optional:
- `TOGGL_API_TOKEN` — Toggl time tracking
- `GROQ_API_KEY` — Groq API (audio transcription)

## Workflows

Workflows are Python modules in `ghost/workflows/`. They're auto-discovered on startup.

Each workflow module should provide either:
- `async run(tg, llm_client, config)` — self-managed async workflow
- `create_agent(tg, config)` — returns `(AgentState, AgentCallbacks)` for the daemon to run

Optional:
- `should_run_check()` — return `False` to skip a scheduled run

Register workflows in `config/config.yaml`:
```yaml
jobs:
  - name: my_workflow
    schedule: "every 30m"
    workflow: my_workflow
    enabled: true
```

## Schedule Syntax

- `"every 30s"` / `"every 5m"` / `"every 2h"` — interval-based
- `"daily 9:00"` — once per day at time
- `"weekdays 6:00"` — Monday-Friday only
- `"on_wake"` — fires when wake event is emitted
- List of schedules: `["on_wake", "every 2h"]`

## Style

- Use `pathlib.Path` over `os.path` for all file operations.
- Async-first: all I/O goes through `asyncio`.
