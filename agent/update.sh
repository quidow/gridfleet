#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Update Script
# Copies new agent code and restarts the service without touching
# the virtualenv, config, Selenium JAR, or host registration.

AGENT_DIR="/opt/gridfleet-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$AGENT_DIR" ]; then
    echo "ERROR: Agent not installed at $AGENT_DIR. Run install.sh first."
    exit 1
fi

echo "=== Updating GridFleet Agent ==="

# Copy agent code
echo "Copying agent_app → $AGENT_DIR/"
sudo cp -r "$SCRIPT_DIR/agent_app" "$AGENT_DIR/"

# Copy project files and update dependencies
echo "Updating dependencies..."
sudo cp "$SCRIPT_DIR/pyproject.toml" "$AGENT_DIR/"
sudo cp "$SCRIPT_DIR/uv.lock" "$AGENT_DIR/" 2>/dev/null || true
cd "$AGENT_DIR" && uv sync --no-dev --frozen

# Restart the service
OS=$(uname -s)
if [ "$OS" = "Linux" ]; then
    echo "Restarting systemd service..."
    sudo systemctl restart gridfleet-agent
    echo "Done. Check: systemctl status gridfleet-agent"
elif [ "$OS" = "Darwin" ]; then
    echo "Restarting launchd service..."
    launchctl kickstart -k "gui/$(id -u)/com.gridfleet.agent"
    echo "Done. Check: curl http://localhost:${AGENT_PORT:-5100}/agent/health"
else
    echo "Unsupported OS: $OS — restart the agent manually."
fi
