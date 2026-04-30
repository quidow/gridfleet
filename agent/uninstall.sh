#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Uninstall Script
# Supports Linux (systemd) and macOS (launchd)

AGENT_DIR="/opt/gridfleet-agent"
CONFIG_DIR="/etc/gridfleet-agent"

echo "=== GridFleet Agent Uninstaller ==="

# Detect OS
OS=$(uname -s)

if [ "$OS" = "Linux" ]; then
    SERVICE="gridfleet-agent"
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        echo "Stopping $SERVICE..."
        sudo systemctl stop "$SERVICE"
    fi
    if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
        echo "Disabling $SERVICE..."
        sudo systemctl disable "$SERVICE"
    fi
    UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
    if [ -f "$UNIT_FILE" ]; then
        echo "Removing $UNIT_FILE"
        sudo rm "$UNIT_FILE"
        sudo systemctl daemon-reload
    fi
    echo "systemd service removed."
    AGENT_PID=$(pgrep -f "uvicorn agent_app.main:app" 2>/dev/null || true)
    if [ -n "$AGENT_PID" ]; then
        echo "Stopping lingering agent process (PID $AGENT_PID)..."
        kill $AGENT_PID 2>/dev/null || true
        sleep 2
        kill -9 $AGENT_PID 2>/dev/null || true
    fi

elif [ "$OS" = "Darwin" ]; then
    PLIST_PATH="$HOME/Library/LaunchAgents/com.gridfleet.agent.plist"
    if [ -f "$PLIST_PATH" ]; then
        echo "Unloading launchd service..."
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm "$PLIST_PATH"
    fi
    echo "launchd service removed."
    AGENT_PID=$(pgrep -f "uvicorn agent_app.main:app" 2>/dev/null || true)
    if [ -n "$AGENT_PID" ]; then
        echo "Stopping agent process (PID $AGENT_PID)..."
        kill $AGENT_PID 2>/dev/null || true
        sleep 2
        kill -9 $AGENT_PID 2>/dev/null || true
    fi
else
    echo "Unsupported OS: $OS"
    exit 1
fi

# Remove agent files
if [ -d "$AGENT_DIR" ]; then
    echo "Removing $AGENT_DIR"
    if [ "$OS" = "Darwin" ]; then
        sudo xattr -rc "$AGENT_DIR" 2>/dev/null || true
    fi
    sudo rm -rf "$AGENT_DIR"
fi

# Remove config
if [ -d "$CONFIG_DIR" ]; then
    echo "Removing $CONFIG_DIR"
    sudo rm -rf "$CONFIG_DIR"
fi

echo "Done. GridFleet Agent has been uninstalled."
