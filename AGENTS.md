# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

GridFleet is a control plane for Appium + Selenium Grid device labs. It is a multi-component monorepo, **not** a single application. Each component has its own toolchain and dependency manifest:

- `backend/` — FastAPI manager, async SQLAlchemy + Postgres, Alembic migrations, leader-owned background loops. Python 3.12, managed by `uv`.
- `agent/` — FastAPI host agent that runs on each device host. Spawns Appium processes and Selenium Grid relay nodes. Python 3.12, managed by `uv`.
- `frontend/` — React 19 + TypeScript + Vite + Tailwind v4 operator dashboard. Node 24, managed by `npm`.
- `testkit/` — supported Python pytest/Appium helper package (`gridfleet_testkit`). Python 3.12, managed by `uv`.
- `driver-packs/` — curated manifests + adapter source. Tarballs are NOT checked in; build with `scripts/build_driver_tarballs.py`.
- `docker/` — `docker-compose.yml` (dev), `docker-compose.prod.yml` (prod), `docker-compose.demo.yml` (frozen demo).

When changing code in one component, run that component's checks. Cross-component changes (e.g. backend/agent contracts, backend/frontend API shapes) need both sides validated.

## Common Commands

Activate component virtualenvs with `uv sync --extra dev` (or the variants below) before first use.

### Backend (`cd backend`)
```bash
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
uv run mypy app/                          # strict mypy + pydantic plugin
uv run pytest -q -n auto                  # parallel via pytest-xdist (~3 min full suite)
# When debugging cross-file failures (xdist state leak), run halves separately — surfaces failures ~2× faster:
#   /bin/ls tests/ | grep "^test_.*\.py$" | head -130 | sed "s|^|tests/|" | xargs uv run pytest -n auto --tb=line -q
uv run pytest -q tests/test_devices_api.py::test_name   # single test
uv run alembic upgrade head               # apply migrations
uv run alembic revision --autogenerate -m "msg"
uv run uvicorn app.main:app --reload      # dev server on :8000
```
Tests marked `db` need a real Postgres (use the docker compose `postgres` service). Coverage threshold is 99% (`pyproject.toml`). Line length 120.

### Agent (`cd agent`)
```bash
uv run ruff format --check agent_app/ tests/
uv run ruff check agent_app/ tests/
uv run mypy agent_app/
uv run pytest -q
uv run uvicorn agent_app.main:app --reload --port 5100
```
The agent has no DB. `test_no_driver_imports.py` enforces that the agent core stays driver-agnostic.

### Frontend (`cd frontend`)
```bash
npm ci
npm run dev                               # Vite on :5173
npm run lint                              # ESLint
npx tsc --noEmit                          # type-check (build also runs `tsc -b`)
npm run build
npm run test                              # Vitest unit
npm run test:e2e:mocked                   # Playwright with mocked backend
npm run test:e2e:live                     # Playwright against live backend+frontend
npm run types:generate                    # dump backend OpenAPI + regenerate src/api/openapi.ts
npm run types:check                       # regenerate and fail if src/api/openapi.ts drifts
```
Live e2e requires backend, Postgres, and the frontend dev server running.

### Testkit
```bash
cd testkit && uv run --extra dev --extra appium pytest -q
```

### Stack
```bash
cd docker && docker compose up --build -d            # full dev stack
./scripts/seed_demo.sh full_demo                     # seed demo DB
./scripts/demo-mode.sh on                            # point backend at demo DB + freeze loops
```

### Driver-pack tarballs
```bash
python3 scripts/build_driver_tarballs.py             # build all curated packs into dist/driver-packs
python3 scripts/build_driver_pack_tarball.py --pack-dir <dir> --out <path> --id <id> --release <ver>
```
Adapter builds require `uv` on `PATH`. Uploaded adapter wheels execute on agent hosts — treat as untrusted RCE surface.

## Architecture: What Spans Multiple Files

### Backend control plane and the leader-loop pattern
The backend is a **stateless multi-worker FastAPI app**; all state is in Postgres. `app/main.py` lifespan starts ~17 leader-owned background loops (heartbeat, session_sync, node_health, device_connectivity, property_refresh, hardware_telemetry, host_resource_telemetry, durable_job_worker, webhook_dispatcher, run_reaper, data_cleanup, session_viability, fleet_capacity, pack_drain, appium_reconciler, device_intent_reconciler, background_loop_flush), plus a keepalive and a non-leader watcher.

Leader election uses **PostgreSQL advisory locks** via `app/core/leader/` (`advisory.py`, `keepalive.py`, `watcher.py`). When adding a new periodic task, follow the existing pattern (lease through the leader, write heartbeats, expose Prometheus gauges) rather than spawning bare `asyncio.create_task` loops. `GRIDFLEET_FREEZE_BACKGROUND_LOOPS=1` skips all of them — the demo compose sets this so seeded state does not drift. Domain-specific loops live under their owning `app/<domain>/services/` package.

### Settings: env vars vs DB registry
There are two distinct config surfaces. Do not conflate them:
- **Process env vars** (`backend/app/core/config.py`, domain config modules such as `backend/app/auth/config.py`, and `agent/agent_app/config.py`) — read once at startup. `GRIDFLEET_DATABASE_URL`, `GRIDFLEET_AUTH_*`, `AGENT_*`, etc.
- **Settings registry** (`app/settings/registry.py`, `app/settings/service.py`) — DB-backed runtime settings editable via the Settings UI. A handful of `GRIDFLEET_*` env vars only seed the *initial* registry default for fresh installs (e.g. `GRIDFLEET_HEARTBEAT_INTERVAL_SEC`, `GRIDFLEET_GRID_HUB_URL`). After the first boot the DB row wins.

See `docs/reference/environment.md` and `docs/reference/settings.md` before adding a new knob.

**Per-domain `BaseSettings` pattern.** Each domain config (`app/<domain>/config.py`) uses `model_config = SettingsConfigDict(env_prefix="", populate_by_name=True, extra="ignore")` with per-field `Field(alias="GRIDFLEET_…")` to preserve ops-facing env-var names. `populate_by_name=True` is **mandatory** — tests construct via field-name kwargs (`AuthConfig(auth_enabled=True)`); without it Pydantic silently drops the kwarg and the test gets the default value.

**Domain config singletons must be reset between tests.** `tests/conftest.py:reset_control_plane_state` snapshots `auth_settings`, `agent_settings`, `packs_settings` and restores at teardown. Adding a new per-domain singleton means extending that fixture; otherwise test mutations leak across the xdist suite (typical symptom: routes return 401 instead of 404 because a stale `auth_enabled=True` persists).

### Driver-pack model (the most important architectural rule)
**Core orchestration must stay driver-agnostic.** Platform-specific behavior — discovery probes, readiness fields, lifecycle actions, capability defaults, health labels — lives in driver-pack manifests under `driver-packs/curated/` and adapter wheels under `driver-packs/adapters/`.

Backend pack pipeline: `app/packs/` (ingest, storage, lifecycle, release, desired-state, dispatch, drain). Agent pack pipeline: `agent/agent_app/pack/` (manifest, runtime, adapter_loader, dispatch, state loop, sidecar_supervisor, tarball_fetch). The agent pulls a desired pack list from the backend, downloads the verified tarball (sha256-pinned), installs it into an isolated `APPIUM_HOME` runtime under `AGENT_RUNTIME_ROOT`, and loads the adapter into a separate venv.

If you find yourself adding `if pack_id == "appium-uiautomator2"` in core code, stop — push it into the manifest or adapter instead. The agent test `test_no_driver_imports.py` actively guards this for the agent side.

### Host agent lifecycle
1. Agent registers with manager (`AGENT_MANAGER_URL`) on a periodic refresh.
2. Backend signals "start node" → agent allocates an Appium port (`AGENT_APPIUM_PORT_RANGE_*`) and a Grid relay port (`AGENT_GRID_NODE_PORT_START`).
3. Agent spawns `appium` from the runtime venv and a Selenium Grid relay (Java + `AGENT_SELENIUM_SERVER_JAR`) pointed at `AGENT_GRID_HUB_URL`.
4. Health checks watch ADB / driver viability and gracefully terminate the Appium process when the device disappears.
5. Backend `node_health_loop` and `device_connectivity_loop` reconcile what the agent reports against DB state.

Sessions go **directly to the Grid hub** (`:4444`); the manager does not proxy WebDriver traffic. The manager owns reservations, run lifecycle, and capability matching; the hub owns request routing.

### Device row locking
Any code that writes `Device.operational_state`, `Device.hold`, or `Device.lifecycle_policy_state` MUST acquire the row lock first via `app.devices.locking.lock_device` (or `lock_devices` for batch) inside the same transaction. Routers should use `get_device_for_update_or_404` for state-mutating endpoints. Background loops (`device_connectivity`, `node_health`, `session_sync`, `session_viability`) commit per device after the locked write window. The leader advisory lock alone is NOT sufficient — API mutators run on every worker and bypass it.

All writes to `Device.operational_state` and `Device.hold` MUST go through `app.devices.services.lifecycle_state_machine.DeviceStateMachine.transition` under the device row lock. The machine routes through `set_operational_state` / `set_hold` so event-bus emissions and `EventLogHook` writes are preserved. Direct attribute assignment (`device.operational_state = ...`) is forbidden outside the writers themselves. See `docs/reference/device-lifecycle.md`.

Writes to `AppiumNode.desired_state`, `AppiumNode.desired_port`, `AppiumNode.transition_token`, and `AppiumNode.transition_deadline` MUST go through `app.appium_nodes.services.desired_state_writer.write_desired_state` under the device row lock. Observation columns (`pid`, `port`, `active_connection_target`, `health_running`, `health_state`, `last_health_checked_at`, `last_observed_at`) are written only by sanctioned observed-state writers: the `app.appium_nodes.services.reconciler*` modules; `app.devices.services.health.apply_node_state_transition` for health fields; `app.devices.services.capability`'s active-target cache fill; verification flows in `app.devices.services.verification_execution`; lifecycle crash handling in `app.devices.services.lifecycle_policy_actions`; and `app.appium_nodes.services.heartbeat`'s `restart_succeeded` event handler. Operator routes and lifecycle flows write desired state only.

### Frontend conventions
- `src/api/` is the only place that talks to the backend. Strongly-typed Axios clients mirror schemas from the backend domain packages under `backend/app/*/schemas*.py`.
- `src/hooks/` wraps `react-query` with explicit polling intervals (5–15s) — operator screens are real-time dashboards, not request/response.
- `src/api/openapi.ts` is generated from backend OpenAPI and committed. Keep backend public API responses on named Pydantic `response_model`s so frontend DTOs can derive from `components["schemas"]`; dynamic JSON-column or third-party subfields may stay flexible inside typed envelopes.
- `src/types/` re-exports stable frontend names derived from `src/api/openapi.ts`. Keep only frontend-only helpers or narrow refinements there; do not hand-copy backend DTOs when a named OpenAPI schema exists.
- Reuse `components/ui/` primitives (`DataTable`, `Badge`, `FilterBar`, `FetchError`) instead of inventing new ones.
- Tailwind-only spacing — no hardcoded pixels. See `docs/guides/frontend-development.md`.

### Auth gate
Off by default for local dev. When `GRIDFLEET_AUTH_ENABLED=true`:
- Backend fails fast at startup unless operator creds, machine creds, and session secret are all set.
- All `/api/*`, `/agent/*`, `/metrics`, `/docs`, `/redoc` are protected. `/health/live`, `/health/ready`, `/api/health` stay open.
- Browser sessions need `X-CSRF-Token` for non-GET; machine clients use Basic auth and skip CSRF.
- Production compose (`docker-compose.prod.yml`) sets auth on and `GRIDFLEET_HOST_AUTO_ACCEPT=false` (operators approve hosts manually).

## Conventions

- **Migrations:** every schema change needs an Alembic revision in `backend/alembic/versions/`. Run `uv run alembic upgrade head` after pulling changes that touch models.
- **Strict typing:** `mypy --strict` on both backend and agent. Pydantic plugin is enabled.
- **Ruff lint set:** `E,F,W,I,N,UP,B,A,BLE,SIM,TCH,RUF,ANN,PLC0415`. SQLAlchemy `Mapped[]` columns under domain model packages are exempt from `TCH003` because runtime types are required.
- **Tests:** pytest-asyncio in `auto` mode. Backend uses `pytest-xdist` (`-n auto`); the `db` marker means "needs the real test database". Frontend unit = Vitest, frontend e2e = Playwright with mocked + live configs.
- **Pre-commit** (`.pre-commit-config.yaml`) runs ruff format + lint + mypy on **both** backend and agent regardless of which component changed. Install hooks once with `pre-commit install`. Both venvs must exist: `cd backend && uv sync --extra dev && cd ../agent && uv sync --extra dev` — otherwise hooks fail silently with `Failed to spawn: ruff`.
- **Conventional Commits:** all commits must use the format `type(scope): description`. Types follow conventional commits, plus `deps` for dependency updates that should be visible to release-please. Scopes: `backend`, `agent`, `frontend`, `testkit`, `docker`, `ci`, `docs`, `deps`, `deps-dev`, `main`. Use `!` for breaking changes: `feat(backend)!: description`. Enforced by commitlint in CI. **Subject must not be sentence-case, start-case, pascal-case, or upper-case** (`feat(backend): foo`, not `feat(backend): Foo`). camelCase identifiers from code (`maxSessions`, `OAuth`) are allowed. **Release-managed scopes** (`backend`, `agent`, `frontend`, `testkit`) accept any conventional type. Only `feat`, `fix`, `perf`, `deps`, and breaking-change marker (`!` or `BREAKING CHANGE`) trigger a release-please version bump; other types (`refactor`, `test`, `chore`, `docs`, `style`, `build`, `ci`, `revert`) land on the branch silently and do not appear in component CHANGELOGs.
- **Versioning:** each component versions independently via release-please. Per-component CHANGELOGs live at `<component>/CHANGELOG.md`. Root `CHANGELOG.md` is a project highlights file. See `docs/reference/release-policy.md`.
- **No real lab data in commits:** no real device IDs, hostnames, credentials, screenshots from private labs, or DB dumps (`CONTRIBUTING.md`).

## Documentation Map

Superpowers-related working docs and plans belong under the project-root `.superpowers/` directory, not under `docs/superpowers/`.

When you need product/operator context that the code does not encode, look here before grepping:

- `docs/reference/architecture.md` — manager, agent, frontend split (canonical)
- `docs/reference/environment.md` — every supported env var
- `docs/reference/settings.md` — DB settings registry surface
- `docs/reference/api.md` — `/api` route catalog
- `docs/reference/capabilities.md` — how Appium caps are derived from device state
- `docs/reference/events-and-webhooks.md` — SSE + webhook event names
- `docs/guides/security.md` — threat model and network boundaries
- `docs/runbooks/` — incident response with exact commands
