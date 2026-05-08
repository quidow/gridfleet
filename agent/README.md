# GridFleet Agent

`gridfleet-agent` is the host-side service that registers a device host with a GridFleet manager, spawns Appium per device, and runs a Selenium Grid relay node so the hub can route WebDriver requests directly to the device.

## Contents
- [Quick install](#quick-install)
- [Prerequisites](#prerequisites)
- [Manual install](#manual-install)
- [Commands](#commands)
- [Configuration reference](#configuration-reference)
- [Logs and service control](#logs-and-service-control)
- [Troubleshooting](#troubleshooting)
- [Security note](#security-note)

## Quick install

Both Linux and macOS use the same bootstrap script. It installs `uv`, fetches Python 3.12, installs `gridfleet-agent`, and runs `gridfleet-agent install --start` with `sudo`.

```bash
# Latest version (development hosts)
curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/install-agent.sh \
    | sh -s -- --start --manager-url http://manager.example.com:8000
```

```bash
# Production: always pin VERSION
VERSION=0.4.0 curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/install-agent.sh \
    | sh -s -- --start --manager-url http://manager.example.com:8000
```

The script reads `$USER` and passes `--user "$USER"` to the sudo'd `gridfleet-agent install`, so the systemd unit runs as the invoking operator, not root.

## Prerequisites

The agent itself only needs Python 3.12, which `uv` fetches automatically — you do not need a system Python install. The following host-level tools are probed during `install`. If any are missing, the installer prints a warning but does not abort; the affected feature simply will not work until the tool is added to the host.

| Tool | Needed for | Detection |
|---|---|---|
| Java 11+ | Selenium Grid relay node | `java -version`, `JAVA_HOME`, `/usr/libexec/java_home` (macOS) |
| Node.js 20+ | Appium per-session | `nvm`, `fnm`, `$PATH` |
| Android SDK platform-tools | ADB-based devices | `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `~/Library/Android/sdk`, `~/Android/Sdk` |

## Manual install

If you prefer not to pipe a script from the internet, install `gridfleet-agent` yourself and run the installer with `sudo` and an explicit operator login.

```bash
# 1. Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. Install the agent (Python 3.12 fetched automatically)
uv tool install --python 3.12 gridfleet-agent==0.4.0

# 3. Provision the service. sudo is required because /opt/gridfleet-agent
#    and /etc/gridfleet-agent are root-owned, and the systemd unit file
#    lives in /etc/systemd/system/. --user makes the service run as you,
#    not as root.
sudo gridfleet-agent install --start \
    --manager-url http://manager.example.com:8000 \
    --user "$USER"
```

On macOS the `sudo` is still required for `/opt` and `/etc`, but the launchd plist is written into your `~/Library/LaunchAgents/` and the agent runs under your `gui/<uid>` domain.

## Commands

### `install`
Provision files, write the systemd unit (Linux) or launchd plist (macOS), optionally start the service. Requires `sudo` and exactly one of `--dry-run`, `--no-start`, `--start`.

```bash
sudo gridfleet-agent install --dry-run --manager-url http://manager.example.com:8000
sudo gridfleet-agent install --no-start --manager-url http://manager.example.com:8000 --user "$USER"
sudo gridfleet-agent install --start    --manager-url http://manager.example.com:8000 --user "$USER"
```

Exit codes: `0` success (including registration pending — printed as a WARNING); `1` files installed but local `/agent/health` failed; `2` invalid args or setup error.

### `status`
Read-only. Reports config file, service file, service active/enabled, local `/agent/health`, resolved `OperatorIdentity`, resolved `uv` path, and the current configured environment with secrets redacted.

```bash
sudo gridfleet-agent status
sudo gridfleet-agent status --user "$USER"
```

### `update`
Drain → upgrade → restart → re-poll health. Requires the service to be running and idle.

```bash
sudo gridfleet-agent update --to 0.4.0
sudo gridfleet-agent update --dry-run
sudo gridfleet-agent update --uv-bin /opt/uv/bin/uv          # advanced
```

Exit codes: `0` success; `1` drain timeout, uv missing for the operator, or post-restart health failure (package was upgraded, but the service is unhealthy); `2` `uv tool upgrade` or restart command failed.

### `uninstall`
```bash
sudo gridfleet-agent uninstall --yes
sudo gridfleet-agent uninstall --yes --keep-config       # leave /etc/gridfleet-agent
sudo gridfleet-agent uninstall --yes --keep-agent-dir    # leave /opt/gridfleet-agent
```

### Service internals: `serve`
`gridfleet-agent serve` is the entrypoint that systemd / launchd invoke; you should not run it by hand. It binds to `0.0.0.0:5100` by default and reads its configuration from `/etc/gridfleet-agent/config.env`. Use `install` and `update` instead.

### `--version`
```bash
gridfleet-agent --version
```

## Configuration reference

All flags below belong to `install`; `status`, `update`, and `uninstall` accept only `--user` (and `update` adds `--uv-bin`).

| Flag | Default | Mapped env var | Notes |
|---|---|---|---|
| `--manager-url` | `http://localhost:8000` | `AGENT_MANAGER_URL` | Manager base URL. |
| `--port` | `5100` | `AGENT_AGENT_PORT` | Agent HTTP port. |
| `--user` | resolved operator | systemd `User=` | See [Manual install](#manual-install). |
| `--manager-auth-username` | none | `AGENT_MANAGER_AUTH_USERNAME` | Required pair with `--manager-auth-password`. |
| `--manager-auth-password` | none | `AGENT_MANAGER_AUTH_PASSWORD` | Required pair with `--manager-auth-username`. |
| `--api-auth-username` | none | `AGENT_API_AUTH_USERNAME` | Required pair with `--api-auth-password`. |
| `--api-auth-password` | none | `AGENT_API_AUTH_PASSWORD` | Required pair with `--api-auth-username`. |
| `--grid-hub-url` | `http://localhost:4444` | `AGENT_GRID_HUB_URL` | Selenium Grid hub. |
| `--grid-publish-url` | `tcp://localhost:4442` | `AGENT_GRID_PUBLISH_URL` | Grid event bus publish. |
| `--grid-subscribe-url` | `tcp://localhost:4443` | `AGENT_GRID_SUBSCRIBE_URL` | Grid event bus subscribe. |
| `--grid-node-port-start` | `5555` | `AGENT_GRID_NODE_PORT_START` | First port allocated to relay nodes. |
| `--selenium-version` | `4.41.0` | jar version | Pinned Selenium server jar. |
| `--enable-web-terminal` | off | `AGENT_ENABLE_WEB_TERMINAL=true` | Requires `--terminal-token`. |
| `--terminal-token` | none | `AGENT_TERMINAL_TOKEN` | Required when web terminal enabled. |

`update` flags:

| Flag | Default | Notes |
|---|---|---|
| `--to <version>` | latest | Pin a specific `gridfleet-agent` package version. |
| `--dry-run` | off | Print the resolved drain URL, uv command (with effective user), restart command, and operator identity. |
| `--user <login>` | resolved | Override operator identity. |
| `--uv-bin <path>` | discovered | Use a specific `uv` binary instead of operator-home discovery. |

## Logs and service control

### Linux
```bash
journalctl -u gridfleet-agent -f
systemctl status gridfleet-agent
sudo systemctl restart gridfleet-agent
```

### macOS
```bash
tail -f /tmp/gridfleet-agent.log
tail -f /tmp/gridfleet-agent.err
launchctl print "gui/$(id -u)/com.gridfleet.agent"
launchctl kickstart -k "gui/$(id -u)/com.gridfleet.agent"   # restart
```

`gui/$(id -u)` is correct **only** when you run these commands as the operator that installed the agent. If you installed via `sudo` from a different shell, replace `$(id -u)` with the operator's uid.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `gridfleet-agent install` raises `PermissionError` on `/opt/gridfleet-agent` | Ran without `sudo` | Re-run with `sudo` and `--user "$USER"` |
| `install` ends with `WARNING: agent registration pending` | Manager requires manual approval or machine auth | Approve in the manager UI, or pass `--manager-auth-username` / `--manager-auth-password` |
| `update` exits `1` with `uv not found for operator …` | Operator's `~/.local/bin/uv` missing | Run `curl -LsSf https://astral.sh/uv/install.sh \| sh` as the operator, or pass `--uv-bin` |
| `update` exits `1` with `update drain timed out` | Sessions still active | Stop new sessions, wait, retry; do not pass `--force` (not implemented) |
| `launchctl print` returns "Could not find service" | Wrong `gui/<uid>` (not the operator) | Run `gridfleet-agent status` to see the resolved operator uid |
| `/agent/health` returns 401 | `--api-auth-*` mismatch between agent and operator's curl | Recheck flags; `status` shows the configured username |
| Port `5100` / `5555+` / `4444` already in use | Another service bound the port | Pick free ports via `--port` / `--grid-node-port-start`, or stop the other service |

## Security note

The quick install pipes a shell script from the `main` branch — pin `VERSION=` for production hosts. `uv tool install` verifies the package wheel against PyPI's hash. Do not run the agent on hosts where uploaded driver-pack adapter wheels are not trusted: those wheels execute in the agent's runtime venv.
