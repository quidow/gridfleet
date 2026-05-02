# Changelog — GridFleet Agent

All notable changes to the GridFleet host agent (`gridfleet-agent` on PyPI) are documented here.

## 0.2.0

### Features

- Rewrite bootstrap installer to use `uv tool install` instead of manual venv creation. Users no longer need Python 3.12+ pre-installed — `uv` handles it.
- Replace `validate_dedicated_venv` with `resolve_bin_path` — the agent no longer requires running from `/opt/gridfleet-agent/venv/bin/`. Supports `uv tool install` paths natively.
- Add `bin_path` to `InstallConfig` for configurable binary resolution in service unit templates (systemd/launchd).
- Replace `pip install --upgrade` with `uv tool upgrade gridfleet-agent` in the update flow.
- Add upgrade awareness: the agent caches version guidance from the manager's registration response and surfaces it on `/agent/health`, `HealthCheckResult.details`, and `gridfleet-agent status` CLI output.
- Use `importlib.metadata` for runtime version resolution — eliminates version sync issues between `pyproject.toml` and source.

### Fixes

- Update CLI tests for removed venv validation guard.
- Close port-allocator and adapter-loader race windows.
- Deduplicate and isolate tarball fetch targets.
- Hold `_start_lock` across `AppiumProcessManager.stop()` body.
- Authenticate agent driver-pack tarball fetches.

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet host agent.
- FastAPI agent that runs on each device host, spawning Appium processes and Selenium Grid relay nodes.
- Driver-pack runtime with manifest-driven adapter loading and isolated APPIUM_HOME.
