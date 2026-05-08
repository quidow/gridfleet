#!/bin/sh
set -eu

# GridFleet Agent — Bootstrap Installer
# Installs uv, uses uv to install Python 3.12 + gridfleet-agent,
# then delegates to 'gridfleet-agent install' for service setup.

VERSION="${VERSION:-latest}"
PACKAGE_SPEC="gridfleet-agent"

if [ "$VERSION" != "latest" ] && [ -n "$VERSION" ]; then
    PACKAGE_SPEC="gridfleet-agent==$VERSION"
fi

# Default to --start unless caller already passed an install mode.
HAS_INSTALL_MODE=0
STOP_EXISTING_SERVICE=1
for arg in "$@"; do
    case "$arg" in
        --dry-run|--no-start)
            HAS_INSTALL_MODE=1
            STOP_EXISTING_SERVICE=0
            break
            ;;
        --start)
            HAS_INSTALL_MODE=1
            ;;
    esac
done

if [ "$HAS_INSTALL_MODE" -eq 0 ]; then
    set -- --start "$@"
fi

# Inject --user so the service does not run as root when the caller omitted it.
HAS_USER=0
for arg in "$@"; do
    case "$arg" in
        --user|--user=*)
            HAS_USER=1
            break
            ;;
    esac
done

if [ "$HAS_USER" -eq 0 ]; then
    OPERATOR_USER="${SUDO_USER:-${USER:-}}"
    if [ -n "$OPERATOR_USER" ] && [ "$OPERATOR_USER" != "root" ]; then
        set -- "$@" --user "$OPERATOR_USER"
    fi
fi

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

# 3. Stop existing service only when the install will start it again.
if [ "$STOP_EXISTING_SERVICE" -eq 1 ]; then
    if [ "$(uname)" = "Darwin" ]; then
        launchctl bootout "gui/$(id -u)/com.gridfleet.agent" 2>/dev/null || true
    else
        systemctl stop gridfleet-agent 2>/dev/null || true
    fi
fi

# 4. Delegate to gridfleet-agent install (needs root for /opt and /etc paths)
AGENT_BIN="$(command -v gridfleet-agent)"
echo ""
if [ "$(id -u)" -ne 0 ]; then
    sudo "$AGENT_BIN" install "$@"
else
    "$AGENT_BIN" install "$@"
fi
