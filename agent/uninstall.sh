#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Uninstall (user-scope, no sudo)

OS="$(uname -s)"
SERVICE="gridfleet-agent"

case "$OS" in
    Linux)
        XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
        XDG_CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
        AGENT_DIR="$XDG_DATA/gridfleet-agent"
        CONFIG_DIR="$XDG_CFG/gridfleet-agent"
        UNIT_FILE="$XDG_CFG/systemd/user/${SERVICE}.service"

        systemctl --user stop "$SERVICE" 2>/dev/null || true
        systemctl --user disable "$SERVICE" 2>/dev/null || true
        if [ -f "$UNIT_FILE" ]; then
            rm "$UNIT_FILE"
            systemctl --user daemon-reload || true
        fi
        ;;
    Darwin)
        AGENT_DIR="$HOME/Library/Application Support/gridfleet-agent"
        CONFIG_DIR="$HOME/Library/Application Support/gridfleet-agent/config"
        PLIST_PATH="$HOME/Library/LaunchAgents/com.gridfleet.agent.plist"
        LOG_DIR="$HOME/Library/Logs/gridfleet-agent"

        launchctl bootout "gui/$(id -u)/com.gridfleet.agent" 2>/dev/null || true
        [ -f "$PLIST_PATH" ] && rm "$PLIST_PATH"
        [ -d "$LOG_DIR" ] && rm -rf "$LOG_DIR"
        ;;
    *)
        echo "Unsupported OS: $OS" >&2
        exit 1
        ;;
esac

[ -d "$AGENT_DIR" ] && rm -rf "$AGENT_DIR"
[ -d "$CONFIG_DIR" ] && rm -rf "$CONFIG_DIR"

echo "GridFleet Agent uninstalled."
