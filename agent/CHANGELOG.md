# Changelog — GridFleet Agent

All notable changes to the GridFleet host agent (`gridfleet-agent` on PyPI) are documented here.

## [0.3.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.4...gridfleet-agent-v0.3.0) (2026-05-05)


### ⚠ BREAKING CHANGES

* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77))

### Bug Fixes

* **backend:** stop transient agent blips from flapping device health ([#61](https://github.com/quidow/gridfleet/issues/61)) ([a58c8e5](https://github.com/quidow/gridfleet/commit/a58c8e5e835b72f5abde69bd078b2868c7cc84d5))


### Code Refactoring

* typed Appium resource claims + structured agent errors ([#77](https://github.com/quidow/gridfleet/issues/77)) ([9bfbc30](https://github.com/quidow/gridfleet/commit/9bfbc300df5fe779f91ba0ba00cc3b8fa2a589e9))

## [0.2.4](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.3...gridfleet-agent-v0.2.4) (2026-05-03)


### Bug Fixes

* **agent:** trigger release for port conflict cleanup ([6a561ca](https://github.com/quidow/gridfleet/commit/6a561ca480c62b9abb2d5141fa98fc4e1a7696b6))

## [0.2.3](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.2...gridfleet-agent-v0.2.3) (2026-05-03)


### Bug Fixes

* **agent:** prioritize node in service path ([c1d2b72](https://github.com/quidow/gridfleet/commit/c1d2b728d5d01a4a0b76f53ca48be8740de17918))

## [0.2.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.1...gridfleet-agent-v0.2.2) (2026-05-03)


### Bug Fixes

* **agent:** prefer nvm node during install ([b0e672b](https://github.com/quidow/gridfleet/commit/b0e672b22e761593c657ae6d54b665f0112a61df))
* **agent:** support sh installer and auth hint ([81cc1fd](https://github.com/quidow/gridfleet/commit/81cc1fd0fb2d344baa135b31665f216d7d607c75))

## [0.2.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.2.0...gridfleet-agent-v0.2.1) (2026-05-02)


### Bug Fixes

* **agent:** close port-allocator and adapter-loader race windows ([#23](https://github.com/quidow/gridfleet/issues/23)) ([4bea799](https://github.com/quidow/gridfleet/commit/4bea799dd6f7931223ec2d2828de5c1e83bf8b8c))
* **agent:** dedup and isolate tarball_fetch targets ([#27](https://github.com/quidow/gridfleet/issues/27)) ([f83ac99](https://github.com/quidow/gridfleet/commit/f83ac991b8b7f9d1916b64fc465187f1995274c7))
* **agent:** hold _start_lock across AppiumProcessManager.stop() body ([#24](https://github.com/quidow/gridfleet/issues/24)) ([a42f1da](https://github.com/quidow/gridfleet/commit/a42f1da759e52add383e9eea0852a85d5633c4e8))
* **agent:** idempotent bootstrap installer with sudo and launchd handling ([#51](https://github.com/quidow/gridfleet/issues/51)) ([db0f059](https://github.com/quidow/gridfleet/commit/db0f059d5288979bbca314fbcf2e92e09e888be8))
* **agent:** reset to 0.2.0, drop --locked from ci ([c6ee2ea](https://github.com/quidow/gridfleet/commit/c6ee2eab4ba7d4b761136cdea1a929d6e22bca3f))
* **agent:** use importlib.metadata for version, fix publish lock files ([b96a112](https://github.com/quidow/gridfleet/commit/b96a112db50ef8e7c8d5bd1524104d7f27cb5afd))
* authenticate agent driver pack tarball fetches ([898859e](https://github.com/quidow/gridfleet/commit/898859eae0ced10a6109058ac6aeab4b6c851934))
* **ci:** update agent lock file, add auto-lockfile workflow, fix local commitlint hook ([920b71e](https://github.com/quidow/gridfleet/commit/920b71eeaa942b33c711a3dcb75115b37525947c))

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
