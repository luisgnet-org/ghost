#!/bin/bash
# install.sh — Ghost one-command setup
#
# Usage:
#   ./install.sh [--home DIR]
#
# Defaults:
#   --home  current directory (the repo root)
#
# What this does:
#   1. Creates a Python venv and installs dependencies
#   2. Sets up .env from .env.example if needed
#   3. Creates runtime directories
#   4. Creates a default agent workspace
#   5. Verifies everything works

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GHOST_HOME="${1:-$SCRIPT_DIR}"

if [[ "${1:-}" == "--home" ]]; then
    GHOST_HOME="${2:-$SCRIPT_DIR}"
fi

# Expand ~
GHOST_HOME="${GHOST_HOME/#\~/$HOME}"

echo ""
echo "  ghost — autonomous agent daemon"
echo "  installing to: $GHOST_HOME"
echo ""

# 1. Python venv
if [ ! -d "$GHOST_HOME/venv" ]; then
    echo "→ Creating Python venv..."
    python3 -m venv "$GHOST_HOME/venv"
fi

echo "→ Installing dependencies..."
"$GHOST_HOME/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# 2. .env
if [ ! -f "$GHOST_HOME/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$GHOST_HOME/.env"
    echo "→ Created .env from template — edit it with your LLM endpoint"
fi

# 3. Runtime directories
mkdir -p "$GHOST_HOME/run/workflows"
mkdir -p "$GHOST_HOME/run/channels"
mkdir -p "$GHOST_HOME/run/sessions"
mkdir -p "$GHOST_HOME/run/pids"
mkdir -p "$GHOST_HOME/agents"

# 4. Default agent workspace
if [ ! -d "$GHOST_HOME/agents/default" ]; then
    echo "→ Creating default agent workspace..."
    cp -r "$SCRIPT_DIR/agent/"* "$GHOST_HOME/agents/default/"
    mkdir -p "$GHOST_HOME/agents/default/memory"
fi

# 5. Verify
echo "→ Verifying..."
export GHOST_HOME
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
if "$GHOST_HOME/venv/bin/python3" -c "from ghost.config import GHOST_HOME; print(f'  GHOST_HOME={GHOST_HOME}')"; then
    echo "✓ ghost installed successfully"
else
    echo "✗ verification failed"
    exit 1
fi

echo ""
echo "  Next steps:"
echo "    1. Edit $GHOST_HOME/.env with your LLM endpoint"
echo "    2. Start: GHOST_HOME=$GHOST_HOME ghost/bin/start.sh"
echo "    3. Send a message: python3 tui/send.py --agent default 'hello'"
echo "    4. Watch replies: python3 tui/watch.py --agent default --follow"
echo ""
