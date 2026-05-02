#!/usr/bin/env bash
set -euo pipefail

AGENT_DIR="/opt/gridfleet-agent"
VENV_DIR="$AGENT_DIR/venv"
VERSION="${VERSION:-latest}"
PACKAGE_SPEC="gridfleet-agent"

if [ "$VERSION" != "latest" ] && [ -n "$VERSION" ]; then
    PACKAGE_SPEC="gridfleet-agent==$VERSION"
fi

INSTALL_ARGS=(--start "$@")
for arg in "$@"; do
    case "$arg" in
        --dry-run|--no-start|--start)
            INSTALL_ARGS=("$@")
            break
            ;;
    esac
done

mkdir -p "$AGENT_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install --upgrade "$PACKAGE_SPEC"
"$VENV_DIR/bin/gridfleet-agent" install "${INSTALL_ARGS[@]}"
