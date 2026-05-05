# Security & Network Boundaries

This guide details the security topology, credentials management, and network exposure boundaries of GridFleet.

## Compose Network Topology

The production setup (`docker-compose.prod.yml`) isolates services logically:

- **Database**: The PostgreSQL container runs *only* on the Docker `internal` network. It does not publish port `5432` to the host machine. The backend reaches it internally.
- **Backend & Selenium Hub**: Both the Backend API and Selenium Hub reside on the `internal` network (to reach the DB or each other) but also publish ports (`8000`, `4444`, `4442`, `4443`) to the host interfaces. These must be reachable by downstream Host Agents and CI runners.
- **Frontend**: The Frontend is served via Nginx and is typically exposed on port `3000`. In a production setting, this is the main Operator entry point and is often placed behind a company-wide reverse proxy or SSO layer.

## Authentication & Authorization

GridFleet supports a login gate for production-style deployments:

- `GRIDFLEET_AUTH_ENABLED=true` is the recommended production-style setting.
- `GRIDFLEET_AUTH_ENABLED=false` keeps the historical lab-trust model for local development and isolated trusted-lab trials only.
- `GRIDFLEET_AUTH_ENABLED=true` enables two auth paths:
  - browser operators sign in through `/api/auth/login` and receive a signed, stateless, HTTP-only session cookie
  - machine clients such as host agents, CI helpers, and metrics scrapers use `Authorization: Basic ...` with the dedicated machine credential pair
- Mutating browser requests must include `X-CSRF-Token`. The frontend injects this automatically after session bootstrap.
- Health probes stay open on `/health/live`, `/health/ready`, and `/api/health`.
- All `/api/*`, `/agent/*`, `/metrics`, `/docs`, `/redoc`, and `/openapi.json` paths are protected when auth is enabled. CI/testkit clients must send machine Basic auth (`GRIDFLEET_TESTKIT_USERNAME` / `GRIDFLEET_TESTKIT_PASSWORD` matching the manager's `GRIDFLEET_MACHINE_AUTH_*` pair) to call reservation, session, or driver-pack catalog endpoints. The Selenium Grid Hub itself remains unauthenticated and lives behind the same network boundary.

Network boundaries still matter even with the auth gate enabled:

- Put the frontend and backend behind TLS. `GRIDFLEET_AUTH_COOKIE_SECURE` should remain `true` in real deployments.
- Restrict direct reachability of the backend and Grid ports with VPN, firewalls, or VPC controls.
- Use a separate machine credential pair for automation and agents; do not reuse the operator username/password.
- Keep host auto-accept disabled in production-style deployments unless every registering host is already controlled and authenticated by the surrounding network.

## Sensitive Credentials Management

Environment variables control the base credentials and routing strings. These should be secured via `.env` files or your orchestration system's secret manager:
- `POSTGRES_PASSWORD`: Defines the PostgreSQL superuser password used by the backend.
- `GRIDFLEET_DATABASE_URL`: Contains the credentials structured as `postgresql+asyncpg://user:password@hostname/...`.
- `GRIDFLEET_AUTH_PASSWORD`: Shared operator password for browser login when the auth gate is enabled.
- `GRIDFLEET_AUTH_SESSION_SECRET`: Signing secret for stateless browser sessions.
- `GRIDFLEET_MACHINE_AUTH_PASSWORD`: Password for machine Basic-auth clients such as agents, CI helpers, and metrics scrapers.

### Inline allocation includes

`POST /api/runs?include=config` and `POST /api/runs/{run_id}/claim?include=config`
embed the device config inline on the response. The inline payload is **always
masked** with the same `MASK_VALUE` (`********`) used by `GET /api/devices/{id}/config?reveal=false`.
There is no `reveal=true` analogue on the include path — callers needing raw
secrets must hit `GET /api/devices/{id}/config?reveal=true` explicitly with the
appropriate auth scope. This keeps the high-traffic claim hot path from becoming
a credentials exfiltration vector if auth scope is misconfigured.

`include=capabilities` is rejected on `POST /api/runs` (HTTP 422,
`details.code = "reserve_capabilities_unsupported"`). On claim,
`live_capabilities` carries the same payload as `GET /api/devices/{id}/capabilities`:
pack-derived defaults plus a live overlay from the agent's `AppiumNode` resource
allocations when the node is in the `running` state. With no running node, the
overlay is empty but the rest of the synthesis (`appium:noReset`, `platformName`,
etc.) still resolves. The field is named `live_capabilities` to mirror internal
terminology — the "live" qualifier covers static pack-derived caps too, not
exclusively probe-time values.

## Webhook Security

The Backend provides event-based webhooks configured by Operators via the UI. When creating webhook endpoints:
- Use HTTPS endpoints to protect payload transmission.
- You can provide an optional secret token when defining the webhook. GridFleet will compute an HMAC SHA-256 signature of the payload using this secret.
- The target system must verify the `x-gridfleet-signature` header to ensure the payload is authentic and originated from your GridFleet instance.

## Host Web Terminal

The host web terminal is an opt-in feature that exposes a PTY-backed shell on any host running the agent, reachable through the GridFleet UI. This is effectively remote code execution on the host, so enable it only when all of the following are true:

- You have a first-party reason to let operators run arbitrary commands on the host through the UI.
- Backend auth (`GRIDFLEET_AUTH_ENABLED=true`) is enabled, so browser sessions are authenticated.
- Manager↔agent transport is trusted (private network, mTLS, VPN — the shared `GRIDFLEET_AGENT_TERMINAL_TOKEN` is an authentication barrier, not a transport layer).
- Host operators understand that `host_terminal_sessions` rows capture session metadata only. No shell transcript is stored.

To enable, set on the backend: `GRIDFLEET_ENABLE_WEB_TERMINAL=true`, `GRIDFLEET_AGENT_TERMINAL_TOKEN=<secret>`, `GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS=<frontend origin>`. The `GRIDFLEET_ENABLE_WEB_TERMINAL` and `GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS` env vars now seed the runtime settings `agent.enable_web_terminal` and `agent.web_terminal_allowed_origins`; admins can flip the toggle and edit the allowlist from the Settings UI without a restart. `GRIDFLEET_AGENT_TERMINAL_TOKEN` remains env-only and must be set before enabling the terminal while auth is on.

On every host agent: `AGENT_ENABLE_WEB_TERMINAL=true`, `AGENT_TERMINAL_TOKEN=<same secret>`.

Restart both services after setting the variables.
