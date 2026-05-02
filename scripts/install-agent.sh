#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Bootstrap Installer
# Installs uv, uses uv to install Python 3.12 + gridfleet-agent,
# then delegates to 'gridfleet-agent install' for service setup.

VERSION="${VERSION:-latest}"
PACKAGE_SPEC="gridfleet-agent"

if [ "$VERSION" != "latest" ] && [ -n "$VERSION" ]; then
    PACKAGE_SPEC="gridfleet-agent==$VERSION"
fi

# Default to --start unless caller already passed an install mode.
INSTALL_ARGS=(--start "$@")
for arg in "$@"; do
    case "$arg" in
        --dry-run|--no-start|--start)
            INSTALL_ARGS=("$@")
            break
            ;;
    esac
done

echo "=== GridFleet Agent Installer ==="
echo "Package: $PACKAGE_SPEC"
echo ""

# 1. Install uv if not present
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Install gridfleet-agent with Python 3.12 via uv
echo "Installing $PACKAGE_SPEC with Python 3.12..."
uv tool install --upgrade --python 3.12 "$PACKAGE_SPEC"

# Ensure gridfleet-agent is on PATH
if ! command -v gridfleet-agent >/dev/null 2>&1; then
    export PATH="$HOME/.local/bin:$PATH"
fi

# 3. Delegate to gridfleet-agent install (needs root for /opt and /etc paths)
AGENT_BIN="$(command -v gridfleet-agent)"
echo ""
if [ "$(id -u)" -ne 0 ]; then
    sudo "$AGENT_BIN" install "${INSTALL_ARGS[@]}"
else
    gridfleet-agent install "${INSTALL_ARGS[@]}"
fi
