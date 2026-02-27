#!/bin/bash
# Start the ghost daemon

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GHOST_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$GHOST_ROOT/.." && pwd)"
cd "$REPO_ROOT"

# Load environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "ERROR: .env file not found at $REPO_ROOT/.env"
    exit 1
fi

# Activate venv
VENV_DIR="$HOME/ghost/venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "ERROR: venv not found at $VENV_DIR"
    exit 1
fi

# Add ghost and agency to Python path
export PYTHONPATH="$GHOST_ROOT:$REPO_ROOT/agency:$PYTHONPATH"

# Ensure run dirs exist
mkdir -p "$HOME/ghost/ghost_run_dir/workflows"

# Start daemon
if [ "$1" = "--no-reload" ]; then
    exec python3 -m ghost.daemon
else
    exec watchmedo auto-restart \
        --patterns='*.py;*.yaml' \
        --recursive \
        --directory="$GHOST_ROOT/ghost" \
        --directory="$GHOST_ROOT/config" \
        --directory="$REPO_ROOT/agency/agency" \
        -- python3 -m ghost.daemon
fi
