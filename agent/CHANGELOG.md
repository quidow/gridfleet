# Changelog — GridFleet Agent

All notable changes to the GridFleet host agent (`gridfleet-agent` on PyPI) are documented here.

## [0.8.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.7.0...gridfleet-agent-v0.8.0) (2026-05-12)


### ⚠ BREAKING CHANGES

* **agent:** remove tool ensure endpoint and management logic

### Features

* **agent:** remove tool ensure endpoint and management logic ([89a581c](https://github.com/quidow/gridfleet/commit/89a581c5221c6272882bf6e4f3ee90b25052cba5))
* remove host tool ensure/version management ([#190](https://github.com/quidow/gridfleet/issues/190)) ([b2562c1](https://github.com/quidow/gridfleet/commit/b2562c16d75ef14c0f4c9131c03151b73f337802))


### Bug Fixes

* **agent:** stop requiring global appium runtime ([ef70225](https://github.com/quidow/gridfleet/commit/ef702257983f6d2ae7a69fae721c1b071b5121a4))

## [0.7.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.2...gridfleet-agent-v0.7.0) (2026-05-11)


### Features

* **agent:** add grid node reregister endpoint ([be50f4c](https://github.com/quidow/gridfleet/commit/be50f4c84eeed7faf61921b82362471ca7044b2b))
* **agent:** default grid node run id to free ([fd88321](https://github.com/quidow/gridfleet/commit/fd883213c8ac6808de93f4e2b9779bdd1e5be257))
* **agent:** reregister grid node with updated stereotype ([8572e08](https://github.com/quidow/gridfleet/commit/8572e084412fad10acac891f1032fcafece0cb14))
* **agent:** support mutable grid node stereotypes ([9715cd6](https://github.com/quidow/gridfleet/commit/9715cd6474e3606e2a8ce0eb3f63b9a7fa0cc172))


### Dependencies

* **deps:** bump pydantic-settings in /agent ([#182](https://github.com/quidow/gridfleet/issues/182)) ([a6abb83](https://github.com/quidow/gridfleet/commit/a6abb83c6367703ca8acc8c0008a2762d0dcc958))

## [0.6.2](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.1...gridfleet-agent-v0.6.2) (2026-05-10)


### Bug Fixes

* **agent:** release adapter-owned doctor refactor ([#165](https://github.com/quidow/gridfleet/issues/165)) ([f3ae257](https://github.com/quidow/gridfleet/commit/f3ae25787e2c8ef926312f11d2313c6513f8bfa9))

## [0.6.1](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.6.0...gridfleet-agent-v0.6.1) (2026-05-10)


### Bug Fixes

* **agent:** split grid node bind host from advertised uri to fix linux docker setups ([#157](https://github.com/quidow/gridfleet/issues/157)) ([8e98a0d](https://github.com/quidow/gridfleet/commit/8e98a0dc28f9d5eedb5aa35566a83e48a6cca4fa))

## [0.6.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.5.0...gridfleet-agent-v0.6.0) (2026-05-10)


### ⚠ BREAKING CHANGES

* **agent:** replace java grid relay with python grid node service ([#148](https://github.com/quidow/gridfleet/issues/148))

### Features

* **agent:** replace java grid relay with python grid node service ([#148](https://github.com/quidow/gridfleet/issues/148)) ([05f7604](https://github.com/quidow/gridfleet/commit/05f760426c3151b2c4b264d0c9e290469f60de28))


### Bug Fixes

* **agent:** close five remaining grid node review findings ([#156](https://github.com/quidow/gridfleet/issues/156)) ([d065ef2](https://github.com/quidow/gridfleet/commit/d065ef24182133b354ce667bb2bc679387326ddc))
* **agent:** match capabilities across always-match + first-match on grid node ([#155](https://github.com/quidow/gridfleet/issues/155)) ([d893655](https://github.com/quidow/gridfleet/commit/d89365546845228be01b45f5b1479c18a790fd4c))
* **agent:** populate session info in grid node status payload ([#153](https://github.com/quidow/gridfleet/issues/153)) ([7a34932](https://github.com/quidow/gridfleet/commit/7a349328000e087219816d8f3e2c03e32b052264))
* **agent:** share httpx async client across probe, registration, and pack-state loops ([#150](https://github.com/quidow/gridfleet/issues/150)) ([874aa11](https://github.com/quidow/gridfleet/commit/874aa1164a86a841fce36240f9711d2733ad8bc6))
* **agent:** tighten grid node lifecycle and capability edge cases ([#154](https://github.com/quidow/gridfleet/issues/154)) ([d5d9a04](https://github.com/quidow/gridfleet/commit/d5d9a047d50c84cc69d982752576a5b1450bff1d))

## [0.5.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.4.0...gridfleet-agent-v0.5.0) (2026-05-10)


### Features

* **main:** add optional icmp ping health check for usb devices with saved ip ([#143](https://github.com/quidow/gridfleet/issues/143)) ([afda5ce](https://github.com/quidow/gridfleet/commit/afda5ce5527167bcd47cb04f227a791ab3cdea1b))

## [0.4.0](https://github.com/quidow/gridfleet/compare/gridfleet-agent-v0.3.0...gridfleet-agent-v0.4.0) (2026-05-08)


### ⚠ BREAKING CHANGES

* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78))

### Features

* **agent:** add --user to status/update/uninstall and --uv-bin to update ([da0636c](https://github.com/quidow/gridfleet/commit/da0636c83dcdad45082f403266ad759dc224c94c))
* **agent:** add operator identity resolver primitive ([575f05f](https://github.com/quidow/gridfleet/commit/575f05ff9d90dd8131eec1e540e73fda7d344558))
* **agent:** add uv runtime discovery and operator-context upgrade command ([a608e12](https://github.com/quidow/gridfleet/commit/a608e1230a76a28e3650fedb9a34ea8394d71079))
* **agent:** enforce optional http basic auth on backend-&gt;agent calls ([#100](https://github.com/quidow/gridfleet/issues/100)) ([00f985e](https://github.com/quidow/gridfleet/commit/00f985e58391b356861a94147b89d502bd57df35))
* **agent:** status reports operator identity and resolved uv path ([2b23756](https://github.com/quidow/gridfleet/commit/2b2375693ca5929c47c6f8321ba11c860d8c1012))


### Bug Fixes

* **agent:** address pr review findings on cli operator path ([67b41c3](https://github.com/quidow/gridfleet/commit/67b41c36041e9d51ae3e30e619260a85e735d3a0))
* **agent:** bootstrap script passes --user so service does not run as root ([a3a4272](https://github.com/quidow/gridfleet/commit/a3a42724b0163eee457d971d848c2d48527e2eae))
* **agent:** chown install artefacts to operator on macos as well as linux ([154f97f](https://github.com/quidow/gridfleet/commit/154f97fae12347bec2f40164dd990269addc7af6))
* **agent:** operator identity through CLI install/update/status ([4e717eb](https://github.com/quidow/gridfleet/commit/4e717ebd2016a6925066079194d394ac466b4f50))
* **agent:** pass operator to status service file resolution ([99ab0b0](https://github.com/quidow/gridfleet/commit/99ab0b089cf262568d3a85b4af3cdcbbfbf58a31))
* **agent:** resolve operator identity before install so service does not run as root ([26fe674](https://github.com/quidow/gridfleet/commit/26fe6748beba353560ea594b7840481ffcf64f4e))
* **agent:** restore current_home branch in uv discovery ([c566add](https://github.com/quidow/gridfleet/commit/c566addb6ad82ee5cf854447a94bb8518833b972))
* **agent:** uninstall uses operator identity for launchctl domain ([0efe41b](https://github.com/quidow/gridfleet/commit/0efe41b7f431c4b403af60f8c39fb67b3efc2815))
* **agent:** update runs uv as operator and maps drain/health failures to exit 1 ([a29bf57](https://github.com/quidow/gridfleet/commit/a29bf5757149011660882bafdfa05fc053a34773))
* **agent:** wrap discover_uv in update cli, fix readme version pin, gate chown on euid ([315f780](https://github.com/quidow/gridfleet/commit/315f780c03de00c904b35926a0674dfb986a31d8))
* **backend,agent:** close 52 codeql alerts ([#115](https://github.com/quidow/gridfleet/issues/115)) ([05190ac](https://github.com/quidow/gridfleet/commit/05190ac32e7be9c2b979513114230f51705a0422))


### Documentation

* **agent:** correct troubleshooting row for registration pending ([0f6b316](https://github.com/quidow/gridfleet/commit/0f6b31691e2c55878edd7b1aa468bd962cef3b80))
* **agent:** rewrite readme to match locked cli spec ([fafd38a](https://github.com/quidow/gridfleet/commit/fafd38a689844db9cadbdb06cfdf8a6c3c194b4a))


### Code Refactoring

* **backend:** derive device health summary on read ([#78](https://github.com/quidow/gridfleet/issues/78)) ([10078ef](https://github.com/quidow/gridfleet/commit/10078ef89dcf12e855776a68002456302c51684c))

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
