# Security & Network Boundaries

This guide details the security topology, credentials management, and network exposure boundaries of GridFleet.

## Compose Network Topology

The production setup (`docker-compose.prod.yml`) isolates services logically:

- **Database**: The PostgreSQL container runs *only* on the Docker `internal` network. It does not publish port `5432` to the host machine. The backend reaches it internally.
- **Backend & WebDriver Router**: Both the Backend API and the Rust WebDriver router reside on the `internal` network (the router reaches the backend over it) but also publish ports (`8000`, `4444`) to the host interfaces. The backend must be reachable by downstream Host Agents; the router (`4444`) must be reachable by CI runners that open WebDriver sessions. Both the router (for WebDriver proxying) and the backend (for the session-observation sweep, viability probes, and force-terminate) reach each host's Appium server directly, so **device-host Appium ports must be reachable from the router AND the backend (manager) hosts — but not from test clients or operators; keep them on the lab network and firewalled from the public internet.**
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
- Protection is applied per-router via the `require_any_auth` dependency, not a global middleware. When auth is enabled, all `/api/*` paths are protected except the auth endpoints themselves (`/api/auth/login`, `/api/auth/session`, `/api/auth/logout`), which must stay reachable to authenticate, and the open health probe `/api/health`. The `/agent/*`, `/metrics`, `/docs`, `/redoc`, and `/openapi.json` paths are likewise protected. CI/testkit clients must send machine Basic auth (`GRIDFLEET_TESTKIT_USERNAME` / `GRIDFLEET_TESTKIT_PASSWORD` matching the manager's `GRIDFLEET_MACHINE_AUTH_*` pair) to call reservation, session, or driver-pack catalog endpoints. The WebDriver router's `:4444` listener does not enforce operator auth on WebDriver traffic and lives behind the same network boundary; the router authenticates its own backend allocation calls with `GRIDFLEET_ROUTER_BACKEND_AUTH`.

Network boundaries still matter even with the auth gate enabled:

- Put the frontend and backend behind TLS. `GRIDFLEET_AUTH_COOKIE_SECURE` should remain `true` in real deployments.
- Restrict direct reachability of the backend, router, and device-host Appium ports with VPN, firewalls, or VPC controls.
- Use a separate machine credential pair for automation and agents; do not reuse the operator username/password.
- Keep host auto-accept disabled in production-style deployments unless every registering host is already controlled and authenticated by the surrounding network.

## Sensitive Credentials Management

Environment variables control the base credentials and routing strings. These should be secured via `.env` files or your orchestration system's secret manager:
- `POSTGRES_PASSWORD`: Defines the PostgreSQL superuser password used by the backend.
- `GRIDFLEET_DATABASE_URL`: Contains the credentials structured as `postgresql+asyncpg://user:password@hostname/...`.
- `GRIDFLEET_AUTH_PASSWORD`: Shared operator password for browser login when the auth gate is enabled.
- `GRIDFLEET_AUTH_SESSION_SECRET`: Signing secret for stateless browser sessions.
- `GRIDFLEET_MACHINE_AUTH_PASSWORD`: Password for machine Basic-auth clients such as agents, CI helpers, and metrics scrapers.

## Host Agent Logs

Host agents ship their own process logs back to the manager for the Host Detail Logs tab. These records are operator diagnostics, not an audit log, and they are retained according to `retention.agent_log_days`.

Treat shipped log lines as sensitive operational data:

- Keep backend auth enabled before exposing the operator UI outside a trusted lab network.
- Do not log credentials, tokens, customer identifiers, or private device data from agent code or driver-pack adapters.

### Backend → agent authentication (optional)

When `AGENT_API_AUTH_USERNAME` / `AGENT_API_AUTH_PASSWORD` are set on each agent and the matching `GRIDFLEET_AGENT_AUTH_USERNAME` / `GRIDFLEET_AGENT_AUTH_PASSWORD` are set on the backend, the agent enforces HTTP Basic on every `/agent/*` HTTP route. Without matching credentials the backend receives 401 and surfaces an agent call failure (circuit breaker tracks consecutive 5xx; 401 is recorded as an agent response error per the existing handling). Leave all four unset for local dev or trusted networks.
