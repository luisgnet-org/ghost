# ghost

Autonomous daemon that runs AI research agents 24/7. Each agent manages
a separate project, compounds research overnight, and you talk to them
from any terminal.

## What it does

- Schedules agent workflows on configurable intervals
- Routes messages via append-only JSONL channels (no external services)
- Spawns and manages opencode sessions (any OpenAI-compatible LLM)
- Task pipeline: design experiments → fan out → collect → postmortem → repeat
- One command install, zero vendor lock-in

## Quick start

```bash
git clone <repo-url>
cd ghost
./install.sh

# Edit .env with your LLM endpoint
vim .env

# Start the daemon
ghost/bin/start.sh

# Send a message to an agent
python3 tui/send.py --agent default "hello"

# Watch for replies
python3 tui/watch.py --agent default --follow
```

## Architecture

```
ghost/                  Python package (daemon)
  daemon.py             Async scheduler loop
  channels.py           Append-only JSONL message bus
  agent_runtime.py      opencode session lifecycle
  scheduler.py          Cron/interval/event parsing
  workflows/            Auto-discovered workflow modules

agent/                  Agent template (copied per project)
  AGENT.md              System prompt
  SOUL/                 Identity, communication style
  bin/                  Agent tools (messages, mem, tasks)

lib/tasks_core.py       Task queue with flock-based concurrency
tui/                    Terminal messaging (send + watch)
config/config.yaml      Job definitions (hot-reloaded)
```

## Configuration

Jobs in `config/config.yaml`:

```yaml
jobs:
  - name: worker_pool
    schedule: "every 5s"
    workflow: worker_pool
    enabled: true
```

Schedule syntax: `"every 5m"`, `"daily 9:00"`, `"weekdays 6:00"`, `"on_wake"`,
or a list: `["on_wake", "every 2h"]`

## Writing workflows

```python
# ghost/workflows/my_workflow.py
async def run(llm_client, config):
    """Called by the daemon on schedule."""
    from ghost.channels import write
    write("my-agent", "research update", from_id="workflow", source="daemon")
```

## Environment

In `.env`:
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` — LLM provider (vLLM, ollama, etc.)
- `GHOST_HOME` — root directory (defaults to repo root)

## License

MIT
