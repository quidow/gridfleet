# GridFleet Agent

`gridfleet-agent` is the host-side service package for GridFleet device hosts.

This package currently provides the runnable agent service entry point:

```bash
gridfleet-agent serve
```

It also provides a safe installer preview:

```bash
gridfleet-agent install --dry-run --manager-url http://manager.example.com:8000
```

The dry run performs host-tool discovery and renders the planned `config.env` and service definition without writing files or starting services.

Real installs use a dedicated virtual environment under `/opt/gridfleet-agent/venv`:

```bash
python3 -m venv /opt/gridfleet-agent/venv
/opt/gridfleet-agent/venv/bin/pip install gridfleet-agent
/opt/gridfleet-agent/venv/bin/gridfleet-agent install --no-start --manager-url http://manager.example.com:8000
```

`--no-start` writes the config and service files but does not enable or start the host service yet.

To also enable/start the service and poll local health:

```bash
/opt/gridfleet-agent/venv/bin/gridfleet-agent install --start --manager-url http://manager.example.com:8000
```

Manager registration verification is not part of the Python installer yet; check the dashboard after local health passes.

Inspect local installation state without changing anything:

```bash
gridfleet-agent status
```

The status command reads `config.env`, checks the local service manager when available, polls local health, and redacts stored secrets.

Upgrade the dedicated agent venv package and restart the host service:

```bash
/opt/gridfleet-agent/venv/bin/gridfleet-agent update --to 0.3.0
```

Use `gridfleet-agent update --dry-run --to 0.3.0` to preview the exact pip upgrade, service restart, and local health-check steps.

Uninstall requires explicit confirmation:

```bash
gridfleet-agent uninstall --yes
```

Use `--keep-config` or `--keep-agent-dir` when you want to preserve local configuration or downloaded runtime state.
