# GridFleet Agent

`gridfleet-agent` is the host-side service that registers a device host with a GridFleet manager, spawns Appium per device, and runs a Selenium Grid relay node so the hub can route WebDriver requests directly to the device.

The agent installs entirely under the operator's home directory — no `sudo` is required to install, start, update, or uninstall.

## Contents
- [Quick install](#quick-install)
- [Prerequisites](#prerequisites)
- [Manual install](#manual-install)
- [Headless Linux: enable linger](#headless-linux-enable-linger)
- [Commands](#commands)
- [Configuration reference](#configuration-reference)
- [Logs and service control](#logs-and-service-control)
- [Migrating from a pre-2026-05-14 install](#migrating-from-a-pre-2026-05-14-install)
- [Troubleshooting](#troubleshooting)
- [Security note](#security-note)

## Quick install

Both Linux and macOS use the same bootstrap script. It installs `uv` if missing, creates a dedicated venv under the per-OS user-scope agent directory, installs `gridfleet-agent` into it, and runs `gridfleet-agent install --start` — all as the invoking operator, no `sudo`.

```bash
# Latest version (development hosts)
curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/install-agent.sh \
    | sh -s -- --start --manager-url http://manager.example.com:8000
```

```bash
# Production: always pin VERSION
curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/install-agent.sh \
    | VERSION=0.10.0 sh -s -- --start --manager-url http://manager.example.com:8000
```

The script refuses to run as root. The service runs as the operator that invoked the script.

## Prerequisites

The agent itself only needs Python 3.12, which `uv` fetches automatically. Host-level tools probed during `install`:

| Tool | Needed for | Detection |
|---|---|---|
| Java 11+ | Android driver build tools | `java -version`, `JAVA_HOME`, `/usr/libexec/java_home` (macOS) |
| Node.js 20+ | Per-pack Appium runtimes | `nvm`, `fnm`, `$PATH` |
| Android SDK platform-tools | ADB-based devices | `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `~/Library/Android/sdk`, `~/Android/Sdk` |

## Manual install

```bash
# 1. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. Create the dedicated venv and install gridfleet-agent.
#    Linux:
AGENT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gridfleet-agent"
#    macOS:
#    AGENT_DIR="$HOME/Library/Application Support/gridfleet-agent"
mkdir -p "$AGENT_DIR"
uv venv --python 3.12 "$AGENT_DIR/venv"
uv pip install --python "$AGENT_DIR/venv/bin/python" "gridfleet-agent==0.10.0"

# 3. Provision the service from the dedicated venv. No sudo.
"$AGENT_DIR/venv/bin/gridfleet-agent" install --start --manager-url http://manager.example.com:8000
```

## Headless Linux: enable linger

A systemd user instance runs only while the user has an active login session. For headless lab hosts, enable lingering once per host so the agent survives logout and reboots:

```bash
sudo loginctl enable-linger "$USER"
```

The installer probes this state after `systemctl --user enable` and prints a warning if linger is off — it does not abort, because desktop / dev hosts where the operator is always logged in do not need it.

## Commands

### `install`
Provision files, write the systemd user unit (Linux) or launchd LaunchAgent plist (macOS), optionally start the service. No `sudo`. Pass exactly one of `--dry-run`, `--no-start`, `--start`.

```bash
gridfleet-agent install --dry-run --manager-url http://manager.example.com:8000
gridfleet-agent install --no-start --manager-url http://manager.example.com:8000
gridfleet-agent install --start    --manager-url http://manager.example.com:8000
```

Exit codes: `0` success (including registration pending — printed as a WARNING); `1` files installed but local `/agent/health` failed; `2` invalid args, setup error, or legacy `/opt`+`/etc` install detected.

### `status`
Read-only. Reports config file, service file, service active/enabled, local `/agent/health`, operator identity, uv path, and the configured environment with secrets redacted.

```bash
gridfleet-agent status
```

### `update`
Drain -> upgrade the dedicated venv in place -> restart -> re-poll health. No `sudo`.

```bash
gridfleet-agent update --to 0.10.0
gridfleet-agent update --dry-run
gridfleet-agent update --uv-bin /path/to/uv          # advanced
```

Exit codes: `0` success; `1` drain timeout or post-restart health failure; `2` `uv pip install --upgrade` or restart command failed.

### `uninstall`
```bash
gridfleet-agent uninstall --yes
gridfleet-agent uninstall --yes --keep-config       # leave config dir in place
gridfleet-agent uninstall --yes --keep-agent-dir    # leave agent dir in place
```

### Service internals: `serve`
`gridfleet-agent serve` is the entrypoint that systemd / launchd invoke; you should not run it by hand.

### `--version`
```bash
gridfleet-agent --version
```

## Configuration reference

All flags below belong to `install`. `status`, `update`, and `uninstall` accept their own subset (`update` adds `--to`, `--dry-run`, `--uv-bin`).

| Flag | Default | Mapped env var | Notes |
|---|---|---|---|
| `--manager-url` | `http://localhost:8000` | `AGENT_MANAGER_URL` | Manager base URL. |
| `--port` | `5100` | `AGENT_AGENT_PORT` | Agent HTTP port. |
| `--advertise-ip` | auto-detect | `AGENT_ADVERTISE_IP` | Hostname or IP the agent advertises to the manager. Set to `host.docker.internal` when the manager runs in Docker on the same host. |
| `--manager-auth-username` | none | `AGENT_MANAGER_AUTH_USERNAME` | Required pair with `--manager-auth-password`. |
| `--manager-auth-password` | none | `AGENT_MANAGER_AUTH_PASSWORD` | Required pair with `--manager-auth-username`. |
| `--api-auth-username` | none | `AGENT_API_AUTH_USERNAME` | Required pair with `--api-auth-password`. |
| `--api-auth-password` | none | `AGENT_API_AUTH_PASSWORD` | Required pair with `--api-auth-username`. |
| `--grid-hub-url` | `http://localhost:4444` | `AGENT_GRID_HUB_URL` | Selenium Grid hub. |
| `--grid-publish-url` | `tcp://localhost:4442` | `AGENT_GRID_PUBLISH_URL` | Grid event bus publish. |
| `--grid-subscribe-url` | `tcp://localhost:4443` | `AGENT_GRID_SUBSCRIBE_URL` | Grid event bus subscribe. |
| `--grid-node-port-start` | `5555` | `AGENT_GRID_NODE_PORT_START` | First port allocated to relay nodes. |
| `--enable-web-terminal` | off | `AGENT_ENABLE_WEB_TERMINAL=true` | Requires `--terminal-token`. |
| `--terminal-token` | none | `AGENT_TERMINAL_TOKEN` | Required when web terminal enabled. |

Install paths (defaults; override with `--agent-dir`, `--config-dir`):

| OS | `agent_dir` | `config_dir` | Service file | Logs |
|---|---|---|---|---|
| Linux | `${XDG_DATA_HOME:-~/.local/share}/gridfleet-agent` | `${XDG_CONFIG_HOME:-~/.config}/gridfleet-agent` | `${XDG_CONFIG_HOME:-~/.config}/systemd/user/gridfleet-agent.service` | journald (user instance) |
| macOS | `~/Library/Application Support/gridfleet-agent` | `~/Library/Application Support/gridfleet-agent/config` | `~/Library/LaunchAgents/com.gridfleet.agent.plist` | `~/Library/Logs/gridfleet-agent/{stdout,stderr}.log` |

`install` also creates a `~/.local/bin/gridfleet-agent` symlink pointing at the dedicated venv so `gridfleet-agent status / update / uninstall` work without typing the full venv path. Add `~/.local/bin` to your `PATH` if it is not there already (`export PATH="$HOME/.local/bin:$PATH"`). If a non-symlink file already exists at that path the installer leaves it untouched and prints a warning. `uninstall` removes the symlink only when it still points into the agent's `agent_dir`.

## Logs and service control

### Linux
```bash
journalctl --user -u gridfleet-agent -f
systemctl --user status gridfleet-agent
systemctl --user restart gridfleet-agent
```

### macOS
```bash
tail -f ~/Library/Logs/gridfleet-agent/stdout.log
tail -f ~/Library/Logs/gridfleet-agent/stderr.log
launchctl print "gui/$(id -u)/com.gridfleet.agent"
launchctl kickstart -k "gui/$(id -u)/com.gridfleet.agent"
```

## Migrating from a pre-2026-05-14 install

Older versions of this installer placed files under `/opt/gridfleet-agent` and `/etc/gridfleet-agent` and required `sudo`. If those paths still exist, `gridfleet-agent install` will refuse to run and tell you to remove them first. Run the one-shot legacy uninstaller:

```bash
curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/uninstall-legacy-agent.sh \
    | sudo sh
```

Then run the normal installer without `sudo`. Agent state is ephemeral (registration with the manager re-happens on next start; runtimes re-download from manager-served tarballs), so there is no data to back up.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `gridfleet-agent install` aborts with `Legacy root-scope install detected` | Old `/opt` or `/etc` paths still present | Run the legacy uninstaller (see migration section), then retry. |
| Install ends with `WARNING: agent registration pending` | Manager requires manual approval or machine auth | Approve in the manager UI, or pass `--manager-auth-username` / `--manager-auth-password`. |
| `WARNING: user-instance linger is off` | Linux headless host without lingering enabled | `sudo loginctl enable-linger "$USER"`. |
| `systemctl --user start gridfleet-agent` says `Failed to connect to bus` | No `$XDG_RUNTIME_DIR`; SSH session has no D-Bus user session | Log in via console or `loginctl enable-linger` so a user systemd instance always runs. |
| `/agent/health` returns 401 | `--api-auth-*` mismatch between agent and operator's curl | Recheck flags; `gridfleet-agent status` shows the configured username. |
| Port `5100` / `5555+` / `4444` already in use | Another service bound the port | Pick free ports via `--port` / `--grid-node-port-start`. |

## Security note

The quick install pipes a shell script from the `main` branch — pin `VERSION=` for production hosts. `uv pip install` verifies the package wheel against PyPI's hash. Do not run the agent on hosts where uploaded driver-pack adapter wheels are not trusted: those wheels execute in the agent's runtime venv.
