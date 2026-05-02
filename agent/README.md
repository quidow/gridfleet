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
