#!/bin/bash
# Stop the ghost daemon
# Kill watchmedo first (it auto-restarts the daemon), then the daemon itself.
pkill -f "watchmedo.*ghost" 2>/dev/null
sleep 1
pkill -f "python.*ghost.daemon" 2>/dev/null
sleep 1
if pgrep -f "ghost.daemon" >/dev/null 2>&1; then
    pkill -9 -f "ghost.daemon" 2>/dev/null
    echo "ghost force-killed"
else
    echo "ghost stopped"
fi
