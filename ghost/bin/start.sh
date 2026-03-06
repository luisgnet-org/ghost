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
if [ -d "${GHOST_VENV:-venv}" ]; then
    source "${GHOST_VENV:-venv}/bin/activate"
else
    echo "ERROR: venv not found at ${GHOST_VENV:-venv}"
    exit 1
fi

# Add ghost to Python path
export PYTHONPATH="$REPO_ROOT:$PYTHONPATH"

# Ensure run dirs exist
mkdir -p "$HOME/ghost/ghost_run_dir/workflows"
mkdir -p "$HOME/ghost/ghost_run_dir/telegram"

# Start daemon
if [ "$1" = "--no-reload" ]; then
    exec python3 -m ghost.daemon
else
    exec watchmedo auto-restart \
        --patterns='*.py;*.yaml' \
        --recursive \
        --directory="$GHOST_ROOT" \
        --directory="$REPO_ROOT/config" \
        -- python3 -m ghost.daemon
fi
