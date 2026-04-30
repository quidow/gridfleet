#!/usr/bin/env bash
set -euo pipefail

# GridFleet Agent — Installation Script
# Supports Linux (systemd) and macOS (launchd)

AGENT_DIR="/opt/gridfleet-agent"
CONFIG_DIR="/etc/gridfleet-agent"
VENV_DIR="$AGENT_DIR/.venv"
AGENT_USER="${AGENT_USER:-$(whoami)}"
AGENT_PORT="${AGENT_PORT:-5100}"
MANAGER_URL="${AGENT_MANAGER_URL:-http://localhost:8000}"
MANAGER_AUTH_USERNAME="${AGENT_MANAGER_AUTH_USERNAME:-}"
MANAGER_AUTH_PASSWORD="${AGENT_MANAGER_AUTH_PASSWORD:-}"
GRID_HUB_URL="${GRID_HUB_URL:-http://localhost:4444}"
GRID_PUBLISH_URL="${GRID_PUBLISH_URL:-tcp://localhost:4442}"
GRID_SUBSCRIBE_URL="${GRID_SUBSCRIBE_URL:-tcp://localhost:4443}"
GRID_NODE_PORT_START="${GRID_NODE_PORT_START:-5555}"
SELENIUM_VERSION="${SELENIUM_VERSION:-4.41.0}"
ENABLE_WEB_TERMINAL="${AGENT_ENABLE_WEB_TERMINAL:-false}"
TERMINAL_TOKEN="${AGENT_TERMINAL_TOKEN:-}"

echo "=== GridFleet Agent Installer ==="
echo "Agent dir:   $AGENT_DIR"
echo "Config dir:  $CONFIG_DIR"
echo "Agent port:  $AGENT_PORT"
echo "Manager URL: $MANAGER_URL"
echo ""

if { [ -n "$MANAGER_AUTH_USERNAME" ] && [ -z "$MANAGER_AUTH_PASSWORD" ]; } || \
   { [ -z "$MANAGER_AUTH_USERNAME" ] && [ -n "$MANAGER_AUTH_PASSWORD" ]; }; then
    echo "ERROR: AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together." >&2
    exit 1
fi

if [ "$ENABLE_WEB_TERMINAL" = "true" ] && [ -z "$TERMINAL_TOKEN" ]; then
    echo "ERROR: AGENT_ENABLE_WEB_TERMINAL=true requires AGENT_TERMINAL_TOKEN." >&2
    exit 1
fi

# Detect OS
OS=$(uname -s)

# Create directories
sudo mkdir -p "$AGENT_DIR" "$CONFIG_DIR" "$AGENT_DIR/runtimes"
sudo chown -R "$AGENT_USER" "$AGENT_DIR" "$CONFIG_DIR"

# Install uv if not present
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Copy agent code
cp -r "$(dirname "$0")"/agent_app "$AGENT_DIR/"
cp "$(dirname "$0")"/pyproject.toml "$AGENT_DIR/"
cp "$(dirname "$0")"/uv.lock "$AGENT_DIR/" 2>/dev/null || true

# Install deps with uv
cd "$AGENT_DIR" && uv sync --no-dev

# --- Download Selenium Server JAR (for Grid 4 relay node) ---
SELENIUM_JAR="$AGENT_DIR/selenium-server.jar"
if [ -f "$SELENIUM_JAR" ]; then
    echo "selenium-server.jar already exists, skipping download."
else
    echo "Downloading selenium-server-${SELENIUM_VERSION}.jar..."
    SELENIUM_URL="https://github.com/SeleniumHQ/selenium/releases/download/selenium-${SELENIUM_VERSION}/selenium-server-${SELENIUM_VERSION}.jar"
    if command -v curl &>/dev/null; then
        curl -fSL -o "$SELENIUM_JAR" "$SELENIUM_URL"
    elif command -v wget &>/dev/null; then
        wget -q -O "$SELENIUM_JAR" "$SELENIUM_URL"
    else
        echo "WARNING: Neither curl nor wget found. Download selenium-server.jar manually to $SELENIUM_JAR"
    fi
    if [ -f "$SELENIUM_JAR" ]; then
        echo "Downloaded selenium-server.jar ($(du -h "$SELENIUM_JAR" | cut -f1))"
    fi
fi

# --- Find Java ---
find_java() {
    # Check PATH
    if command -v java &>/dev/null; then
        command -v java
        return
    fi
    # Check JAVA_HOME
    if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
        echo "$JAVA_HOME/bin/java"
        return
    fi
    # sdkman
    local sdkman_java="$HOME/.sdkman/candidates/java/current/bin/java"
    if [ -x "$sdkman_java" ]; then
        echo "$sdkman_java"
        return
    fi
    # macOS java_home
    if [ "$OS" = "Darwin" ] && [ -x /usr/libexec/java_home ]; then
        local jh
        jh=$(/usr/libexec/java_home 2>/dev/null) || true
        if [ -n "$jh" ] && [ -x "$jh/bin/java" ]; then
            echo "$jh/bin/java"
            return
        fi
    fi
    # Common locations
    for p in /usr/local/bin/java /usr/bin/java; do
        if [ -x "$p" ]; then
            echo "$p"
            return
        fi
    done
    echo ""
}

JAVA_BIN=$(find_java)
if [ -n "$JAVA_BIN" ]; then
    JAVA_DIR=$(dirname "$JAVA_BIN")
    JAVA_VERSION=$("$JAVA_BIN" -version 2>&1 | head -1)
    echo "Found Java: $JAVA_BIN ($JAVA_VERSION)"
else
    JAVA_DIR=""
    echo "WARNING: Java not found. Grid relay node will not start."
    echo "Install Java 11+ and ensure it's on PATH, in JAVA_HOME, or managed by sdkman."
fi

# --- Build PATH for the agent service ---
# launchd/systemd provide minimal PATH; we need appium, adb, and java visible.
build_service_path() {
    local svc_path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    # Add Java
    if [ -n "$JAVA_DIR" ]; then
        svc_path="$JAVA_DIR:$svc_path"
    fi

    # Add nvm node (pick latest version)
    local nvm_dir="$HOME/.nvm/versions/node"
    if [ -d "$nvm_dir" ]; then
        local latest
        latest=$(ls -d "$nvm_dir"/v* 2>/dev/null | sort -t. -k1,1rn -k2,2rn -k3,3rn | head -1)
        if [ -n "$latest" ] && [ -d "$latest/bin" ]; then
            svc_path="$latest/bin:$svc_path"
            echo "Found nvm Node: $latest" >&2
        fi
    fi

    # Add fnm default Node. Services do not load interactive shell init,
    # so discover the concrete bin directory at install time.
    local fnm_bin=""
    for candidate in "$(command -v fnm 2>/dev/null || true)" /usr/local/bin/fnm "$HOME/.local/bin/fnm"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            fnm_bin="$candidate"
            break
        fi
    done
    if [ -n "$fnm_bin" ]; then
        local fnm_node=""
        fnm_node=$("$fnm_bin" exec --using default which node 2>/dev/null || true)
        if [ -n "$fnm_node" ] && [ -x "$fnm_node" ]; then
            local fnm_node_dir
            fnm_node_dir=$(dirname "$fnm_node")
            svc_path="$fnm_node_dir:$svc_path"
            echo "Found fnm Node: $fnm_node_dir" >&2
        else
            local fnm_default_bins=(
                "${FNM_DIR:-}/aliases/default/bin"
                "${XDG_DATA_HOME:-$HOME/.local/share}/fnm/aliases/default/bin"
                "$HOME/.local/share/fnm/aliases/default/bin"
                "$HOME/Library/Application Support/fnm/aliases/default/bin"
            )
            for fnm_node_dir in "${fnm_default_bins[@]}"; do
                if [ -n "$fnm_node_dir" ] && [ -x "$fnm_node_dir/node" ]; then
                    svc_path="$fnm_node_dir:$svc_path"
                    echo "Found fnm Node: $fnm_node_dir" >&2
                    break
                fi
            done
        fi
    fi

    # Add Android SDK platform-tools
    local android_sdk_roots=(
        "${ANDROID_HOME:-}"
        "${ANDROID_SDK_ROOT:-}"
        "$HOME/Library/Android/sdk"
        "$HOME/Android/Sdk"
        "/opt/android-sdk"
        "/usr/local/android-sdk"
    )
    for sdk in "${android_sdk_roots[@]}"; do
        if [ -n "$sdk" ] && [ -d "$sdk/platform-tools" ]; then
            svc_path="$sdk/platform-tools:$svc_path"
            echo "Found Android SDK: $sdk" >&2
            # Export for use outside function
            DETECTED_ANDROID_HOME="$sdk"
            break
        fi
    done

    echo "$svc_path"
}

DETECTED_ANDROID_HOME=""

SERVICE_PATH=$(build_service_path)

# Write config
{
cat <<EOF
AGENT_MANAGER_URL=$MANAGER_URL
AGENT_AGENT_PORT=$AGENT_PORT
AGENT_GRID_HUB_URL=$GRID_HUB_URL
AGENT_GRID_PUBLISH_URL=$GRID_PUBLISH_URL
AGENT_GRID_SUBSCRIBE_URL=$GRID_SUBSCRIBE_URL
AGENT_SELENIUM_SERVER_JAR=$SELENIUM_JAR
AGENT_GRID_NODE_PORT_START=$GRID_NODE_PORT_START
PATH=$SERVICE_PATH
EOF
if [ -n "$DETECTED_ANDROID_HOME" ]; then
    echo "ANDROID_HOME=$DETECTED_ANDROID_HOME"
    echo "ANDROID_SDK_ROOT=$DETECTED_ANDROID_HOME"
fi
if [ -n "$MANAGER_AUTH_USERNAME" ]; then
    echo "AGENT_MANAGER_AUTH_USERNAME=$MANAGER_AUTH_USERNAME"
    echo "AGENT_MANAGER_AUTH_PASSWORD=$MANAGER_AUTH_PASSWORD"
fi
if [ "$ENABLE_WEB_TERMINAL" = "true" ]; then
    echo "AGENT_ENABLE_WEB_TERMINAL=true"
    if [ -n "$TERMINAL_TOKEN" ]; then
        echo "AGENT_TERMINAL_TOKEN=$TERMINAL_TOKEN"
    fi
fi
} > "$CONFIG_DIR/config.env"

if [ "$OS" = "Linux" ]; then
    # systemd service
    sudo tee /etc/systemd/system/gridfleet-agent.service > /dev/null <<EOF
[Unit]
Description=GridFleet Agent
After=network.target

[Service]
Type=simple
User=$AGENT_USER
WorkingDirectory=$AGENT_DIR
EnvironmentFile=$CONFIG_DIR/config.env
ExecStart=$AGENT_DIR/.venv/bin/uvicorn agent_app.main:app --host 0.0.0.0 --port $AGENT_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable gridfleet-agent
    sudo systemctl start gridfleet-agent
    echo "Agent installed as systemd service. Check: systemctl status gridfleet-agent"

elif [ "$OS" = "Darwin" ]; then
    # launchd plist
    PLIST_PATH="$HOME/Library/LaunchAgents/com.gridfleet.agent.plist"

    # Build optional ANDROID_HOME env entries
    ANDROID_PLIST_ENTRIES=""
    if [ -n "$DETECTED_ANDROID_HOME" ]; then
        ANDROID_PLIST_ENTRIES="        <key>ANDROID_HOME</key>
        <string>$DETECTED_ANDROID_HOME</string>
        <key>ANDROID_SDK_ROOT</key>
        <string>$DETECTED_ANDROID_HOME</string>"
    fi

    MANAGER_AUTH_PLIST_ENTRIES=""
    if [ -n "$MANAGER_AUTH_USERNAME" ]; then
        MANAGER_AUTH_PLIST_ENTRIES="        <key>AGENT_MANAGER_AUTH_USERNAME</key>
        <string>$MANAGER_AUTH_USERNAME</string>
        <key>AGENT_MANAGER_AUTH_PASSWORD</key>
        <string>$MANAGER_AUTH_PASSWORD</string>"
    fi

    TERMINAL_PLIST_ENTRIES=""
    if [ "$ENABLE_WEB_TERMINAL" = "true" ]; then
        TERMINAL_PLIST_ENTRIES="        <key>AGENT_ENABLE_WEB_TERMINAL</key>
        <string>true</string>"
        if [ -n "$TERMINAL_TOKEN" ]; then
            TERMINAL_PLIST_ENTRIES="$TERMINAL_PLIST_ENTRIES
        <key>AGENT_TERMINAL_TOKEN</key>
        <string>$TERMINAL_TOKEN</string>"
        fi
    fi

    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gridfleet.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$AGENT_DIR/.venv/bin/uvicorn</string>
        <string>agent_app.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>$AGENT_PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$AGENT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
        <key>AGENT_MANAGER_URL</key>
        <string>$MANAGER_URL</string>
        <key>AGENT_AGENT_PORT</key>
        <string>$AGENT_PORT</string>
        <key>AGENT_GRID_HUB_URL</key>
        <string>$GRID_HUB_URL</string>
        <key>AGENT_GRID_PUBLISH_URL</key>
        <string>$GRID_PUBLISH_URL</string>
        <key>AGENT_GRID_SUBSCRIBE_URL</key>
        <string>$GRID_SUBSCRIBE_URL</string>
        <key>AGENT_SELENIUM_SERVER_JAR</key>
        <string>$SELENIUM_JAR</string>
        <key>AGENT_GRID_NODE_PORT_START</key>
        <string>$GRID_NODE_PORT_START</string>
$ANDROID_PLIST_ENTRIES
$MANAGER_AUTH_PLIST_ENTRIES
$TERMINAL_PLIST_ENTRIES
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/gridfleet-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/gridfleet-agent.err</string>
</dict>
</plist>
EOF
    launchctl load "$PLIST_PATH"
    echo "Agent installed as launchd service. Check: launchctl list | grep gridfleet"
else
    echo "Unsupported OS: $OS"
    exit 1
fi

echo ""
echo "Verifying agent registration..."
sleep 3

HEALTH_URL="http://localhost:${AGENT_PORT}/agent/health"
if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
    echo "Agent is running."
    if [ -n "$MANAGER_AUTH_USERNAME" ]; then
        REG_CHECK=$(curl -sf -u "$MANAGER_AUTH_USERNAME:$MANAGER_AUTH_PASSWORD" "${MANAGER_URL}/api/hosts" 2>/dev/null | grep -c "$(hostname)" || true)
    else
        REG_CHECK=$(curl -sf "${MANAGER_URL}/api/hosts" 2>/dev/null | grep -c "$(hostname)" || true)
    fi
    if [ "$REG_CHECK" -gt 0 ]; then
        echo "Agent successfully registered with manager."
    else
        echo "Agent is running but registration may be pending. Check the dashboard."
    fi
else
    echo "WARNING: Agent health check failed. Check logs for details."
fi

echo ""
echo "Done. Agent installed and will self-register with manager at $MANAGER_URL."
echo "Visit the dashboard to approve the host and confirm devices."
echo ""
echo "  - Selenium Server: $SELENIUM_JAR"
[ -n "$JAVA_BIN" ] && echo "  - Java: $JAVA_BIN"
echo "  - Service PATH includes: appium, adb, java directories"
