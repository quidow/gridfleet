#!/bin/sh
set -eu

# GridFleet Agent — Bootstrap Installer (user-scope, no sudo)
# Installs uv (if missing), creates a dedicated venv under the per-OS user-scope
# agent directory, installs gridfleet-agent into it, then runs
# `gridfleet-agent install` from that venv.

VERSION="${VERSION:-latest}"
PACKAGE_SPEC="gridfleet-agent"

if [ "$VERSION" != "latest" ] && [ -n "$VERSION" ]; then
    PACKAGE_SPEC="gridfleet-agent==$VERSION"
fi

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: do not run this installer as root. Run as the operator who will own the agent service." >&2
    exit 2
fi

OS="$(uname -s)"
case "$OS" in
    Linux)
        AGENT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gridfleet-agent"
        ;;
    Darwin)
        AGENT_DIR="$HOME/Library/Application Support/gridfleet-agent"
        ;;
    *)
        echo "Unsupported OS: $OS" >&2
        exit 2
        ;;
esac

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

echo "=== GridFleet Agent Installer ==="
echo "Package: $PACKAGE_SPEC"
echo "Agent dir: $AGENT_DIR"
echo

# 1. Install uv if not present.
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Create the dedicated venv and install gridfleet-agent into it.
mkdir -p "$AGENT_DIR"
if [ ! -x "$AGENT_DIR/venv/bin/python" ]; then
    echo "Creating dedicated venv at $AGENT_DIR/venv..."
    uv venv --python 3.12 "$AGENT_DIR/venv"
fi
echo "Installing $PACKAGE_SPEC into $AGENT_DIR/venv..."
uv pip install --python "$AGENT_DIR/venv/bin/python" --upgrade "$PACKAGE_SPEC"

# 3. Stop existing user-scope service if we're about to start it again.
if [ "$STOP_EXISTING_SERVICE" -eq 1 ]; then
    if [ "$OS" = "Darwin" ]; then
        launchctl bootout "gui/$(id -u)/com.gridfleet.agent" 2>/dev/null || true
    else
        systemctl --user stop gridfleet-agent 2>/dev/null || true
    fi
fi

# 4. Delegate to the agent CLI from the dedicated venv.
"$AGENT_DIR/venv/bin/gridfleet-agent" install "$@"
