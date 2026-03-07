#!/bin/bash
# Start the ghost daemon

# Self-locate: this script is at GHOST_HOME/git/ghost/ghost/bin/start.sh
# Walk up: bin/ → ghost/ (package) → ghost/ (repo) → git/ → GHOST_HOME/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GHOST_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"       # ghost Python package dir
REPO_ROOT="$(cd "$GHOST_ROOT/.." && pwd)"        # git/ghost repo root
GHOST_HOME="${GHOST_HOME:-$(cd "$REPO_ROOT/../.." && pwd)}"
export GHOST_HOME
cd "$REPO_ROOT"

# Load environment — .env lives at GHOST_HOME root (no paths inside it)
if [ -f "$GHOST_HOME/.env" ]; then
    set -a; source "$GHOST_HOME/.env"; set +a
elif [ -f "$REPO_ROOT/.env" ]; then
    set -a; source "$REPO_ROOT/.env"; set +a
else
    echo "ERROR: .env not found at $GHOST_HOME/.env or $REPO_ROOT/.env"
    exit 1
fi

# Derive venv from GHOST_HOME — avoid 'source activate' (breaks after dir renames)
VENV="$GHOST_HOME/venv"
PYTHON="$VENV/bin/python3"
WATCHMEDO="$VENV/bin/watchmedo"

if [ ! -f "$PYTHON" ]; then
    echo "ERROR: Python not found at $PYTHON"
    echo "  Run: python3 -m venv $VENV && $VENV/bin/pip install -r $REPO_ROOT/requirements.txt"
    exit 1
fi

# Add ghost to Python path
export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

# Ensure run dirs exist
mkdir -p "$GHOST_HOME/ghost_run_dir/workflows"
mkdir -p "$GHOST_HOME/ghost_run_dir/telegram"

# Start daemon
if [ "${1:-}" = "--no-reload" ]; then
    exec "$PYTHON" -m ghost.daemon
else
    exec "$WATCHMEDO" auto-restart \
        --patterns='*.py;*.yaml' \
        --recursive \
        --directory="$GHOST_ROOT" \
        --directory="$REPO_ROOT/config" \
        -- "$PYTHON" -m ghost.daemon
fi
