# GridFleet Agent

`gridfleet-agent` is the host-side service package for GridFleet device hosts.

## Quick Install

```bash
curl -LsSf https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/install-agent.sh | sh -s -- \
  --manager-url http://manager.example.com:8000
```

This installs [uv](https://docs.astral.sh/uv/) if missing, fetches Python 3.12 automatically, and sets up the agent as a system service. No pre-installed Python version required.

## Commands

Preview what the installer will do without writing files:

```bash
gridfleet-agent install --dry-run --manager-url http://manager.example.com:8000
```

Install without starting the service:

```bash
gridfleet-agent install --no-start --manager-url http://manager.example.com:8000
```

Install and start the service:

```bash
gridfleet-agent install --start --manager-url http://manager.example.com:8000
```

Check installation status:

```bash
gridfleet-agent status
```

Upgrade to a specific version:

```bash
gridfleet-agent update --to 0.3.0
```

Or upgrade via uv directly:

```bash
uv tool upgrade gridfleet-agent
sudo systemctl restart gridfleet-agent  # Linux
launchctl kickstart -k gui/$(id -u)/com.gridfleet.agent  # macOS
```

Uninstall (requires confirmation):

```bash
gridfleet-agent uninstall --yes
```

Use `--keep-config` or `--keep-agent-dir` to preserve local configuration or runtime state.
