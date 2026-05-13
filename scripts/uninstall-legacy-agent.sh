#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Legacy Uninstall (root-scope install layout)
# Cleans up the pre-2026-05-14 /opt + /etc install. Run once, with sudo.

AGENT_DIR="/opt/gridfleet-agent"
CONFIG_DIR="/etc/gridfleet-agent"

OS=$(uname -s)

if [ "$OS" = "Linux" ]; then
    SERVICE="gridfleet-agent"
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        sudo systemctl stop "$SERVICE"
    fi
    if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
        sudo systemctl disable "$SERVICE"
    fi
    UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
    if [ -f "$UNIT_FILE" ]; then
        sudo rm "$UNIT_FILE"
        sudo systemctl daemon-reload
    fi
    AGENT_PID=$(pgrep -f "uvicorn agent_app.main:app" 2>/dev/null || true)
    if [ -n "$AGENT_PID" ]; then
        kill $AGENT_PID 2>/dev/null || true
        sleep 2
        kill -9 $AGENT_PID 2>/dev/null || true
    fi
elif [ "$OS" = "Darwin" ]; then
    PLIST_PATH="$HOME/Library/LaunchAgents/com.gridfleet.agent.plist"
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm "$PLIST_PATH"
    fi
    AGENT_PID=$(pgrep -f "uvicorn agent_app.main:app" 2>/dev/null || true)
    if [ -n "$AGENT_PID" ]; then
        kill $AGENT_PID 2>/dev/null || true
        sleep 2
        kill -9 $AGENT_PID 2>/dev/null || true
    fi
else
    echo "Unsupported OS: $OS"
    exit 1
fi

if [ -d "$AGENT_DIR" ]; then
    if [ "$OS" = "Darwin" ]; then
        sudo xattr -rc "$AGENT_DIR" 2>/dev/null || true
    fi
    sudo rm -rf "$AGENT_DIR"
fi

if [ -d "$CONFIG_DIR" ]; then
    sudo rm -rf "$CONFIG_DIR"
fi

echo "Legacy GridFleet Agent install removed. Re-run install without sudo."
