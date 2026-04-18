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

# 5. Install opencode if not present
if ! command -v opencode &>/dev/null; then
    echo "→ Installing opencode..."
    curl -fsSL https://opencode.ai/install | bash
fi

# 6. Platform service setup
detect_platform() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}

PLATFORM="$(detect_platform)"
echo "→ Platform: $PLATFORM"

install_systemd_service() {
    local service_file="/etc/systemd/system/ghost.service"
    local venv_python="$GHOST_HOME/venv/bin/python3"

    if [ -f "$service_file" ]; then
        echo "  systemd service already exists, skipping"
        return
    fi

    cat > /tmp/ghost.service <<UNIT
[Unit]
Description=ghost autonomous agent daemon
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
Environment=GHOST_HOME=$GHOST_HOME
Environment=PYTHONPATH=$SCRIPT_DIR
ExecStart=$venv_python -m ghost.daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

    if [ "$(id -u)" -eq 0 ]; then
        mv /tmp/ghost.service "$service_file"
        systemctl daemon-reload
        systemctl enable ghost.service
        echo "  systemd service installed and enabled"
        echo "  start with: systemctl start ghost"
    else
        echo "  systemd unit written to /tmp/ghost.service"
        echo "  install with: sudo mv /tmp/ghost.service $service_file && sudo systemctl daemon-reload && sudo systemctl enable ghost"
    fi
}

install_launchd_service() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist="$plist_dir/com.ghost.daemon.plist"
    local venv_python="$GHOST_HOME/venv/bin/python3"

    if [ -f "$plist" ]; then
        echo "  launchd plist already exists, skipping"
        return
    fi

    mkdir -p "$plist_dir"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ghost.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$venv_python</string>
        <string>-m</string>
        <string>ghost.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GHOST_HOME</key>
        <string>$GHOST_HOME</string>
        <key>PYTHONPATH</key>
        <string>$SCRIPT_DIR</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$GHOST_HOME/run/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>$GHOST_HOME/run/daemon.err</string>
</dict>
</plist>
PLIST

    echo "  launchd plist installed at $plist"
    echo "  start with: launchctl load $plist"
}

case "$PLATFORM" in
    linux)  install_systemd_service ;;
    macos)  install_launchd_service ;;
    *)      echo "  unknown platform — start manually: ghost/bin/start.sh" ;;
esac

# 7. Verify
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
if [ "$PLATFORM" = "linux" ]; then
    echo "    2. Start: systemctl start ghost"
elif [ "$PLATFORM" = "macos" ]; then
    echo "    2. Start: launchctl load ~/Library/LaunchAgents/com.ghost.daemon.plist"
else
    echo "    2. Start: GHOST_HOME=$GHOST_HOME ghost/bin/start.sh"
fi
echo "    3. Send a message: python3 tui/send.py --agent default 'hello'"
echo "    4. Watch replies: python3 tui/watch.py --agent default --follow"
echo ""
