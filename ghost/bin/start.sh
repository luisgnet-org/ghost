#!/bin/bash
# Start the ghost daemon

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GHOST_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$GHOST_ROOT/.." && pwd)"
GHOST_HOME="${GHOST_HOME:-$REPO_ROOT}"
export GHOST_HOME
cd "$REPO_ROOT"

# Load environment
if [ -f "$GHOST_HOME/.env" ]; then
    set -a; source "$GHOST_HOME/.env"; set +a
elif [ -f "$REPO_ROOT/.env" ]; then
    set -a; source "$REPO_ROOT/.env"; set +a
fi

# Python — use venv if available, fall back to system
if [ -f "$GHOST_HOME/venv/bin/python3" ]; then
    PYTHON="$GHOST_HOME/venv/bin/python3"
elif [ -f "$REPO_ROOT/venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/venv/bin/python3"
else
    PYTHON="python3"
fi

export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

# Ensure run dirs
mkdir -p "$GHOST_HOME/run/workflows"
mkdir -p "$GHOST_HOME/run/channels"

exec "$PYTHON" -m ghost.daemon
