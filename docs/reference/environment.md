# Environment Reference

This page documents the shipped environment-variable surface for the manager and host agent.

Use this page to answer "is this a real process env var?" before adding it to production `.env` files or service definitions.

## Backend Core Process Variables

These are read directly by `backend/app/config.py`.

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `GRIDFLEET_DATABASE_URL` | `postgresql+asyncpg://gridfleet:gridfleet@localhost:5432/gridfleet` | backend process | Required in production compose; SQLAlchemy async connection URL |
| `GRIDFLEET_DB_POOL_SIZE` | `10` | backend process | Base SQLAlchemy connection pool size |
| `GRIDFLEET_DB_MAX_OVERFLOW` | `20` | backend process | Additional burst connections allowed above pool size |
| `GRIDFLEET_REQUEST_TIMEOUT_SEC` | `30` | backend process | Default timeout for outbound backend HTTP calls |
| `GRIDFLEET_ENV` | unset | backend logging | `dev` / `development` / `local` switches logs to console format; other values keep JSON logs |
| `GRIDFLEET_AUTH_ENABLED` | `false` | backend auth gate | Enables browser login cookies and machine Basic auth for protected manager routes. Production compose sets this to `true`; leave it `false` only for local development or isolated trusted-lab trials. |
| `GRIDFLEET_AUTH_USERNAME` | unset | backend auth gate | Shared operator username used by `/api/auth/login` when auth is enabled |
| `GRIDFLEET_AUTH_PASSWORD` | unset | backend auth gate | Shared operator password used by `/api/auth/login` when auth is enabled |
| `GRIDFLEET_AUTH_SESSION_SECRET` | unset | backend auth gate | HMAC signing secret for stateless browser sessions; required when auth is enabled |
| `GRIDFLEET_AUTH_SESSION_TTL_SEC` | `28800` | backend auth gate | Browser session lifetime in seconds (8 hours by default) |
| `GRIDFLEET_AUTH_COOKIE_SECURE` | `true` | backend auth gate | Marks the browser session cookie as `Secure`; keep enabled behind HTTPS |
| `GRIDFLEET_MACHINE_AUTH_USERNAME` | unset | backend auth gate | Basic-auth username accepted for machine clients such as agents, CI helpers, and metrics scrapers |
| `GRIDFLEET_MACHINE_AUTH_PASSWORD` | unset | backend auth gate | Basic-auth password accepted for machine clients such as agents, CI helpers, and metrics scrapers |
| `GRIDFLEET_AGENT_AUTH_USERNAME` | unset | backend process | Username sent on backend → agent calls. Required together with `GRIDFLEET_AGENT_AUTH_PASSWORD`. |
| `GRIDFLEET_AGENT_AUTH_PASSWORD` | unset | backend process | Password sent on backend → agent calls. Required together with `GRIDFLEET_AGENT_AUTH_USERNAME`. |
| `GRIDFLEET_ENABLE_WEB_TERMINAL` | `false` | runtime setting default | Initial value for `agent.enable_web_terminal`. Admins can flip this in the Settings UI at runtime; the env var only seeds the default for fresh installs. Must be `true` on both manager and agent for sessions to succeed. |
| `GRIDFLEET_AGENT_TERMINAL_TOKEN` | unset | backend web-terminal proxy | Shared secret sent to the agent as `X-Agent-Terminal-Token` when proxying a terminal session. Required when the terminal is enabled and `GRIDFLEET_AUTH_ENABLED=true`; the backend rejects enabling the terminal from the Settings UI while this is unset under those conditions. |
| `GRIDFLEET_AGENT_TERMINAL_SCHEME` | `ws` | backend web-terminal proxy | WebSocket scheme used from the backend proxy to host agents. Use `wss` only when agents terminate TLS directly or sit behind a TLS WebSocket endpoint reachable at the registered host IP and agent port. |
| `GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS` | empty | runtime setting default | Initial value for `agent.web_terminal_allowed_origins`. Editable in the Settings UI at runtime. Empty means "block all when auth is enabled". |
| `GRIDFLEET_FREEZE_BACKGROUND_LOOPS` | unset | backend process | Truthy value (`1`/`true`/`yes`/`on`) skips all 15 leader-owned background loops (leader keepalive, heartbeat, session sync, node health, device connectivity, property refresh, hardware/host telemetry, durable jobs, webhook delivery, run reaper, data cleanup, session viability, fleet capacity, pack drain). Intended for frozen demo databases so seeded state does not drift. Set automatically by `docker-compose.demo.yml`. |

## Backend Settings-Registry Fallback Variables

These are not the authoritative settings store. They only provide the initial session default when the settings registry has no DB override for that key.

| Variable | Registry key | Default | Notes |
| --- | --- | --- | --- |
| `GRIDFLEET_HEARTBEAT_INTERVAL_SEC` | `general.heartbeat_interval_sec` | `15` | Agent heartbeat loop cadence |
| `GRIDFLEET_MAX_MISSED_HEARTBEATS` | `general.max_missed_heartbeats` | `3` | Missed heartbeats before host becomes offline |
| `GRIDFLEET_SESSION_QUEUE_TIMEOUT_SEC` | `general.session_queue_timeout_sec` | `300` | Grid session queue timeout |
| `GRIDFLEET_DEVICE_COOLDOWN_MAX_SEC` | `general.device_cooldown_max_sec` | `3600` | Maximum run-scoped device cooldown accepted from clients |
| `GRIDFLEET_DEVICE_COOLDOWN_ESCALATION_THRESHOLD` | `general.device_cooldown_escalation_threshold` | `3` | Seeds the registry default for fresh installs; `0` disables escalation |
| `GRIDFLEET_PROPERTY_REFRESH_INTERVAL_SEC` | `general.property_refresh_interval_sec` | `600` | Property refresh cadence |
| `GRIDFLEET_GRID_HUB_URL` | `grid.hub_url` | `http://selenium-hub:4444` | Grid hub URL used by the backend |
| `GRIDFLEET_APPIUM_PORT_RANGE_START` | `appium.port_range_start` | `4723` | Managed Appium port range start |
| `GRIDFLEET_APPIUM_PORT_RANGE_END` | `appium.port_range_end` | `4823` | Managed Appium port range end |
| `GRIDFLEET_MIN_AGENT_VERSION` | `agent.min_version` | `0.1.0` | Empty string disables minimum-version enforcement |
| `GRIDFLEET_AGENT_RECOMMENDED_VERSION` | `agent.recommended_version` | empty | Seeds initial `agent.recommended_version` setting default (empty = disabled). |
| `GRIDFLEET_HOST_AUTO_ACCEPT` | `agent.auto_accept_hosts` | `true` | Auto-approve self-registering hosts. Production compose sets this to `false` so operators approve new hosts explicitly. |
| `GRIDFLEET_RUN_REAPER_INTERVAL_SEC` | `reservations.reaper_interval_sec` | `15` | Stale-run reaper cadence |

For the full registry surface, including DB-backed settings that do not have env fallbacks, see [settings.md](settings.md).

## Agent Process Variables

These are read directly by `agent/agent_app/config.py`.

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `AGENT_MANAGER_URL` | `http://localhost:8000` | agent process | Backend base URL used for registration and manager-owned calls |
| `AGENT_REGISTRATION_REFRESH_INTERVAL_SEC` | `30` | agent process | How often the agent re-registers to refresh mutable host fields such as IP address and capabilities |
| `AGENT_MANAGER_AUTH_USERNAME` | unset | agent process | Optional Basic-auth username used for manager API calls when the backend auth gate is enabled |
| `AGENT_MANAGER_AUTH_PASSWORD` | unset | agent process | Optional Basic-auth password used for manager API calls when the backend auth gate is enabled |
| `AGENT_API_AUTH_USERNAME` | unset | agent process | Optional Basic-auth username; required together with `AGENT_API_AUTH_PASSWORD`. When set, the agent enforces HTTP Basic on all `/agent/*` HTTP routes. WebSocket terminal still requires `AGENT_TERMINAL_TOKEN`. |
| `AGENT_API_AUTH_PASSWORD` | unset | agent process | Optional Basic-auth password; required together with `AGENT_API_AUTH_USERNAME`. |
| `AGENT_AGENT_PORT` | `5100` | agent process | Agent listen port |
| `AGENT_GRID_HUB_URL` | `http://selenium-hub:4444` | agent process | Grid hub URL used for manager-provided launch specs |
| `AGENT_GRID_PUBLISH_URL` | `tcp://localhost:4442` | agent process | Grid event-bus publish URL |
| `AGENT_GRID_SUBSCRIBE_URL` | `tcp://localhost:4443` | agent process | Grid event-bus subscribe URL |
| `AGENT_GRID_NODE_HEARTBEAT_SEC` | `5` | agent process | Seconds between Python Grid Node `NODE_STATUS` heartbeats. |
| `AGENT_GRID_NODE_SESSION_TIMEOUT_SEC` | `300` | agent process | Maximum session age before the Python Grid Node asks Appium to delete the session and frees the slot. |
| `AGENT_GRID_NODE_PROXY_TIMEOUT_SEC` | `60` | agent process | Upstream Appium request timeout for proxied Grid Node HTTP commands. |
| `AGENT_RUNTIME_ROOT` | `/opt/gridfleet-agent/runtimes` | agent process | Root directory where the agent installs isolated Appium runtime environments (`APPIUM_HOME` per `runtime_id`). Must be writable by the agent user; the Python installer creates it during `gridfleet-agent install`. |
| `AGENT_APPIUM_PORT_RANGE_START` | `4723` | agent process | Start of Appium server port range |
| `AGENT_APPIUM_PORT_RANGE_END` | `4823` | agent process | End of Appium server port range |
| `AGENT_GRID_NODE_PORT_START` | `5555` | agent process | First Python Grid Node HTTP port assigned on the host |
| `AGENT_GRID_NODE_BIND_HOST` | `0.0.0.0` | agent process | Local interface the Python Grid Node HTTP server binds. Independent of `AGENT_ADVERTISE_IP` (which is what the hub registers). The default wildcard works for every supported topology including docker-compose with `host.docker.internal` advertised. Set to a specific IP to restrict the listening interface. |
| `AGENT_ADB_RECONNECT_PORT` | `5555` | agent process | Default Android reconnect port |
| `AGENT_ADVERTISE_IP` | unset | agent process | Optional externally reachable address advertised by the agent during registration and Python Grid Node startup. Accepts **any DNS name or IP** the backend and Selenium Hub can reach, not strictly an IPv4 address. Useful for co-located docker deployments where the host's LAN IP is unreachable from containers (e.g. set to `host.docker.internal` or `172.17.0.1`). Leave empty to use UDP-trick discovery. |
| `AGENT_ENABLE_WEB_TERMINAL` | `false` | agent process | Opt-in flag for the host web terminal. When `true`, the agent accepts authenticated `WS /agent/terminal` connections from the manager. |
| `AGENT_TERMINAL_TOKEN` | unset | agent process | Shared secret that must match `GRIDFLEET_AGENT_TERMINAL_TOKEN`. Required when `AGENT_ENABLE_WEB_TERMINAL=true`. |
| `AGENT_TERMINAL_SHELL` | unset | agent process | Optional override for the PTY shell program. Defaults to the `SHELL` env var or `/bin/sh`. |

## Agent Installer Helper Variables

These are consumed by `scripts/install-agent.sh` or `gridfleet-agent install` while creating the host service definition. They are not the same thing as the agent settings above.

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `VERSION` | `latest` | bootstrap wrapper | Optional PyPI version pinned via `uv tool install gridfleet-agent==VERSION` by `scripts/install-agent.sh` |
| `--user` | current shell user | installer CLI | Service account / file owner |
| `--port` | `5100` | installer CLI | Convenience input that becomes process `AGENT_AGENT_PORT` |
| `--manager-url` | `http://localhost:8000` | installer CLI | Written into the generated service env/config |
| `--manager-auth-username` | unset | installer CLI | Optional machine-auth username written into the generated service env/config |
| `--manager-auth-password` | unset | installer CLI | Optional machine-auth password written into the generated service env/config |
| `--grid-hub-url` | `http://localhost:4444` | installer CLI | Convenience input that becomes process `AGENT_GRID_HUB_URL` |
| `--grid-publish-url` | `tcp://localhost:4442` | installer CLI | Convenience input that becomes process `AGENT_GRID_PUBLISH_URL` |
| `--grid-subscribe-url` | `tcp://localhost:4443` | installer CLI | Convenience input that becomes process `AGENT_GRID_SUBSCRIBE_URL` |
| `--grid-node-port-start` | `5555` | installer CLI | Convenience input that becomes process `AGENT_GRID_NODE_PORT_START` |

## External Client Variables

These variables are consumed by the supported testkit and example CI helpers.

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `GRID_URL` | `http://localhost:4444` | testkit / examples | Selenium Grid hub URL |
| `GRIDFLEET_API_URL` | `http://localhost:8000/api` | testkit / examples | GridFleet API base used for run helpers, config lookup, and session reporting |

## Host Tool Discovery Variables

These are not GridFleet settings-registry keys. They are host-level environment inputs that the installer or agent-side tool discovery honors while finding Java and the Android SDK.

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `JAVA_HOME` | unset | installer + agent subprocess env | If set to a valid JDK root, the installer and process Java discovery use it before falling back to common paths |
| `ANDROID_HOME` | unset | installer + agent tool discovery | Preferred Android SDK root input; the installer writes it into generated service env when it detects a valid SDK |
| `ANDROID_SDK_ROOT` | unset | installer + agent tool discovery | Alternate Android SDK root input; treated the same way as `ANDROID_HOME` |

Notes:

- `gridfleet-agent install` detects a usable Android SDK root and writes `ANDROID_HOME` and `ANDROID_SDK_ROOT` into the generated service environment when it finds one. The same flow is available through `scripts/install-agent.sh`.
- The agent process also forwards the detected Android SDK root into Appium subprocesses so drivers can find the SDK.
- Java does not have a dedicated `AGENT_*` settings. The agent resolves `java` from `PATH`, `JAVA_HOME`, sdkman, and common system locations.
- When `GRIDFLEET_AUTH_ENABLED=true`, the backend fails fast at startup unless all operator, machine-auth, and session-secret variables are present.

## Production Files

- Manager stack example: [`docker/.env.example`](../../docker/.env.example)
- Production compose file: [`docker/docker-compose.prod.yml`](../../docker/docker-compose.prod.yml)
- Backup script: [`scripts/backup.sh`](../../scripts/backup.sh)
- Restore script: [`scripts/restore.sh`](../../scripts/restore.sh)
