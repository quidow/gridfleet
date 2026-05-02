# Changelog — GridFleet Agent

All notable changes to the GridFleet host agent (`gridfleet-agent` on PyPI) are documented here.

## [0.2.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.1...gridfleet-agent-v0.2.2) (2026-05-02)


### Bug Fixes

* **agent:** use importlib.metadata for version, fix publish lock files ([b96a112](https://github.com/quidow/gridfleet/commit/b96a112db50ef8e7c8d5bd1524104d7f27cb5afd))
* **ci:** update agent lock file, add auto-lockfile workflow, fix local commitlint hook ([920b71e](https://github.com/quidow/gridfleet/commit/920b71eeaa942b33c711a3dcb75115b37525947c))

## [0.2.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.0...gridfleet-agent-v0.2.1) (2026-05-02)


### Bug Fixes

* **agent:** close port-allocator and adapter-loader race windows ([#23](https://github.com/quidow/gridfleet/issues/23)) ([4bea799](https://github.com/quidow/gridfleet/commit/4bea799dd6f7931223ec2d2828de5c1e83bf8b8c))
* **agent:** dedup and isolate tarball_fetch targets ([#27](https://github.com/quidow/gridfleet/issues/27)) ([f83ac99](https://github.com/quidow/gridfleet/commit/f83ac991b8b7f9d1916b64fc465187f1995274c7))
* **agent:** hold _start_lock across AppiumProcessManager.stop() body ([#24](https://github.com/quidow/gridfleet/issues/24)) ([a42f1da](https://github.com/quidow/gridfleet/commit/a42f1da759e52add383e9eea0852a85d5633c4e8))
* authenticate agent driver pack tarball fetches ([898859e](https://github.com/quidow/gridfleet/commit/898859eae0ced10a6109058ac6aeab4b6c851934))

## 0.2.0

### Features

- Rewrite bootstrap installer to use `uv tool install` instead of manual venv creation. Users no longer need Python 3.12+ pre-installed — `uv` handles it.
- Replace `validate_dedicated_venv` with `resolve_bin_path` — the agent no longer requires running from `/opt/gridfleet-agent/venv/bin/`. Supports `uv tool install` paths natively.
- Add `bin_path` to `InstallConfig` for configurable binary resolution in service unit templates (systemd/launchd).
- Replace `pip install --upgrade` with `uv tool upgrade gridfleet-agent` in the update flow.
- Add upgrade awareness: the agent caches version guidance from the manager's registration response and surfaces it on `/agent/health`, `HealthCheckResult.details`, and `gridfleet-agent status` CLI output.

### Fixes

- Update CLI tests for removed venv validation guard.

## 0.1.0 — Initial Public Preview

- Initial public preview of the GridFleet host agent.
- FastAPI agent that runs on each device host, spawning Appium processes and Selenium Grid relay nodes.
- Driver-pack runtime with manifest-driven adapter loading and isolated APPIUM_HOME.
