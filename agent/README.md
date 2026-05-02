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

Host installation, Selenium JAR setup, and systemd/launchd integration are still handled by the repository shell scripts.
