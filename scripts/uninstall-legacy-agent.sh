#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Legacy Uninstall (root-scope install layout)
# Cleans up the pre-2026-05-14 /opt + /etc install. Run once, with sudo.

AGENT_DIR="/opt/gridfleet-agent"
CONFIG_DIR="/etc/gridfleet-agent"

OS=$(uname -s)

# Resolve invoking operator's home so we can clean per-user uv tool installs that
# were laid down with sudo (which leaves root-owned __pycache__ in their dirs).
if [ -n "${SUDO_USER:-}" ]; then
    OP_USER="$SUDO_USER"
    OP_HOME=$(eval echo "~${SUDO_USER}")
else
    OP_USER="$(id -un)"
    OP_HOME="$HOME"
fi

kill_agent_processes() {
    local pids
    # Match both legacy uvicorn-direct launches and the post-CLI `gridfleet-agent serve` form.
    pids=$(pgrep -f "uvicorn agent_app.main:app" 2>/dev/null || true)
    pids="$pids $(pgrep -f "gridfleet-agent serve" 2>/dev/null || true)"
    pids=$(echo "$pids" | tr ' ' '\n' | sort -u | grep -v '^$' || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}

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
    kill_agent_processes
elif [ "$OS" = "Darwin" ]; then
    OP_UID=$(id -u "$OP_USER")
    PLIST_PATH="${OP_HOME}/Library/LaunchAgents/com.gridfleet.agent.plist"
    # Bootout by domain-target so the registration is removed even if the plist
    # file has already been deleted (otherwise the service stays loaded forever).
    sudo -u "$OP_USER" launchctl bootout "gui/${OP_UID}/com.gridfleet.agent" 2>/dev/null || true
    if [ -f "$PLIST_PATH" ]; then
        sudo -u "$OP_USER" launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
    fi
    kill_agent_processes
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

# Clean root-owned residue inside the per-user uv tool dir if present.
# Sudo installs of `uv tool install gridfleet-agent` write __pycache__ entries as root,
# which then block a subsequent `uv tool uninstall` run as the operator with EACCES.
UV_TOOL_DIR="${OP_HOME}/.local/share/uv/tools/gridfleet-agent"
if [ -d "$UV_TOOL_DIR" ] && [ -n "$(sudo find "$UV_TOOL_DIR" -uid 0 -print -quit 2>/dev/null)" ]; then
    sudo rm -rf "$UV_TOOL_DIR"
    UV_BIN_SHIM="${OP_HOME}/.local/bin/gridfleet-agent"
    if [ -L "$UV_BIN_SHIM" ] || [ -f "$UV_BIN_SHIM" ]; then
        sudo rm -f "$UV_BIN_SHIM"
    fi
fi

echo "Legacy GridFleet Agent install removed. Re-run install without sudo."
