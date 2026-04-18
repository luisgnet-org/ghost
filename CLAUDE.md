# ghost

Standalone autonomous daemon. Schedules agent workflows, routes messages via
JSONL channels, manages opencode agent sessions. No external dependencies.

## Structure

```
ghost/                              # repo root
├── ghost/                          # Python package (daemon)
│   ├── daemon.py                   #   Async scheduler loop
│   ├── scheduler.py                #   Schedule parsing (cron, interval, event)
│   ├── config.py                   #   Configuration + path management
│   ├── channels.py                 #   Append-only JSONL message bus
│   ├── agent_runtime.py            #   opencode session lifecycle
│   ├── event_log.py                #   Structured event logging
│   ├── workflows/                  #   Auto-discovered workflow modules
│   │   ├── worker_pool.py          #     Task board → agent dispatch
│   │   └── daemon_status.py        #     Health heartbeat
│   └── bin/                        #   Start/stop scripts
├── agent/                          # Agent template (copied per instance)
│   ├── AGENT.md                    #   System prompt for opencode
│   ├── SOUL/                       #   Identity, comms style
│   ├── bin/                        #   Agent tools (messages, mem, tasks)
│   └── memory/                     #   Session logs
├── lib/                            # Shared libraries
│   └── tasks_core.py              #   Task queue (flock-based concurrency)
├── tui/                            # Terminal messaging interface
│   ├── send.py                     #   Write message to channel
│   └── watch.py                    #   Tail channels, display messages
├── config/config.yaml              # Job definitions (hot-reloaded)
├── install.sh                      # One-command setup
└── requirements.txt
```

## Setup

```bash
./install.sh
# Edit .env with your LLM endpoint
ghost/bin/start.sh
```

## Environment

In `.env`:
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` — LLM provider (vLLM, ollama, etc.)
- `GHOST_HOME` — root directory (defaults to repo root)
- `GHOST_RUNS_DIR` — runtime data (defaults to `$GHOST_HOME/run`)

## Workflows

Auto-discovered from `ghost/workflows/`. Each module provides:
- `async run(llm_client, config)` — workflow entry point

Optional:
- `should_run_check()` — return `False` to skip

## Schedule Syntax

- `"every 30s"` / `"every 5m"` / `"every 2h"` — interval-based
- `"daily 9:00"` — once per day
- `"weekdays 6:00"` — Monday-Friday
- `"on_wake"` — event-driven
- List: `["on_wake", "every 2h"]`

## Channels

Communication via append-only JSONL files in `run/channels/`. Agents and TUI
both read/write the same files. No server needed — pure filesystem.

```python
from ghost.channels import write, read, poll
write("agent_name", "hello", from_id="user", source="tui")
```

## Style

- `pathlib.Path` over `os.path`.
- Async-first: all I/O through `asyncio`.
