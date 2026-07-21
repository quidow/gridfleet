# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

GridFleet is a control plane for Appium device labs. It is a multi-component monorepo, **not** a single application. Each component has its own toolchain and dependency manifest:

- `backend/` — FastAPI manager, async SQLAlchemy + Postgres, Alembic migrations, background loops run in a dedicated scheduler process. Python 3.14, managed by `uv`.
- `agent/` — FastAPI host agent that runs on each device host. Spawns Appium processes per device. Python 3.14, managed by `uv`.
- `router/` — Pingora-based Rust W3C WebDriver router that listens on `:4444`, allocates a device via the backend internal grid API, and proxies session commands directly to Appium on the allocated host. Replaces the Selenium Grid hub.
- `frontend/` — React 19 + TypeScript + Vite + Tailwind v4 operator dashboard. Node 24, managed by `npm`.
- `testkit/` — supported Python pytest/Appium helper package (`gridfleet_testkit`). Python 3.12, managed by `uv`.
- `driver-packs/` — curated manifests + adapter source. Tarballs are NOT checked in; build with `scripts/build_driver_tarballs.py`.
- `docker/` — `docker-compose.yml` (dev), `docker-compose.prod.yml` (prod).

When changing code in one component, run that component's checks. Cross-component changes (e.g. backend/agent contracts, backend/frontend API shapes) need both sides validated.

## Common Commands

Activate component virtualenvs with `uv sync --extra dev` (or the variants below) before first use.

### Backend (`cd backend`)
```bash
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
uv run mypy app/                          # strict mypy + pydantic plugin
uv run vulture app/ .vulture_whitelist.py --min-confidence 80   # dead-code check (CI enforces this)
uv run pytest -q -n auto                  # parallel via pytest-xdist (~3 min full suite)
# When debugging cross-file failures (xdist state leak), run halves separately — surfaces failures ~2× faster:
#   /bin/ls tests/ | grep "^test_.*\.py$" | head -130 | sed "s|^|tests/|" | xargs uv run pytest -n auto --tb=line -q
uv run pytest -q tests/test_devices_api.py::test_name   # single test
uv run alembic upgrade head               # apply migrations
uv run alembic revision --autogenerate -m "msg"
uv run uvicorn app.main:app --reload      # dev server on :8000
```
Tests marked `db` need a real Postgres (use the docker compose `postgres` service). Line length 120.

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
npx tsc -b tsconfig.app.json              # type-check — NOT `tsc --noEmit`, see below
npm run build
npm run test                              # Vitest unit
npm run test:e2e:mocked                   # Playwright with mocked backend
npm run test:e2e:live                     # Playwright against live backend+frontend
npm run types:generate                    # dump backend OpenAPI + regenerate src/api/openapi.ts
npm run types:check                       # regenerate and fail if src/api/openapi.ts drifts
```
Live e2e requires backend, Postgres, and the frontend dev server running.

**`npx tsc --noEmit` is a no-op here.** Root `tsconfig.json` is solution-style (project
references, no `files`/`include`), so that command type-checks nothing and exits 0 — it
will "pass" over code that does not compile. Use `npx tsc -b tsconfig.app.json` or
`npm run build`; CI type-checks via the build.

### Testkit
```bash
cd testkit && uv run --extra dev pytest -q
```

### Stack
```bash
cd docker && docker compose up --build -d            # full dev stack
```

### Driver-pack tarballs
```bash
python3 scripts/build_driver_tarballs.py             # build all curated packs into dist/driver-packs
python3 scripts/build_driver_pack_tarball.py --pack-dir <dir> --out <path> --id <id> --release <ver>
```
Adapter builds require `uv` on `PATH`. Uploaded adapter wheels execute on agent hosts — treat as untrusted RCE surface.

## Architecture: What Spans Multiple Files

### Backend control plane and the scheduler-loop pattern
The backend is a **stateless multi-worker FastAPI app**; all state is in Postgres. `app/main.py` lifespan starts 6 background loops (host_sweep, appium_sweep, durable_job_worker, grid_allocation_reaper, device_intent_reconciler, janitor). They run in exactly one **scheduler process** — the prod `backend-scheduler` Compose service (one worker), or the API process itself in local/single-container runs — gated by `GRIDFLEET_RUN_BACKGROUND_LOOPS` (default `true`). A stall watchdog `os._exit(70)`s the scheduler when a loop wedges so the supervisor restarts it. Agents push one consolidated `POST /agent/hosts/status` per host on `AGENT_STATUS_PUSH_INTERVAL_SEC` (default 10 s) — nodes, restart events, start failures, pack status, host telemetry, and agent version/capabilities in a single payload; pack status has no separate channel. The push handler commits liveness first, then ingests restart events, converges Appium nodes, and folds the observation sections into durable facts with per-stage containment. `host_sweep` only detects silence from push recency (`general.host_offline_after_sec`, default 45 s), emits liveness edges and the offline cascade, and runs the cadence-gated `/agent/health` partition diagnostic (60 s plumbing constant). The latest `status_push.host_status` snapshot remains for host diagnostics and device-capability active-target fill. The agent gathers observations locally on fixed cadences (30/60/300/600 s constants in `agent_app/probes.py`) against a roster pulled from `GET /agent/devices/probe-targets`; fact latency is push-bounded after each probe fires. The backend dials an agent only to act, never to observe: desired-state wake hints, pack lifecycle-state polls, link-repair dispatch (+ one post-repair re-probe), presence enumeration on probe-miss, verification flows, and the partition diagnostic. The `grid_allocation_reaper_loop` expires stale queue tickets and fails crash-orphaned pending rows (session creation is backend-owned; there is no router confirm step). The `device_intent_reconciler` tick GCs expired deny intents and clears elapsed reservation-row cooldowns before its full reconcile scan. `appium_sweep` runs the direct-to-Appium session observation pass (liveness probes + orphan-session kill via Appium's `/appium/sessions`) on every tick or doorbell wake, then runs the scheduled session-viability pass behind a 60 s scan throttle.

A single **PostgreSQL advisory lock** (`app/core/leader/advisory.py`, singleton `control_plane_leader`, lock id `6001`, held for the process lifetime) guards against an accidental second loop-runner — it is a launch guard, **not** a leader election. There is no heartbeat, watcher, or preemption; failover is restart-based (compose `restart: unless-stopped` re-acquires the lock) plus the in-process stall watchdog. There is a second, unrelated advisory lock: `app/core/locks.py`'s transaction-scoped `group_mutation` lock serialises every writer of `device_groups` *definitions* (`create_group`, `update_group`, `delete_group`, and the portability importer's group-insert block). It is what keeps a concurrent create from leaving a dynamic group's `filters.member_of` pointing at a deleted static group — `FOR UPDATE` cannot, because it is blind to rows a peer transaction has not inserted yet. Membership writes (`add_members`, `remove_members`) deliberately do not take it and keep their own `device_groups` row lock via `_get_group_row(..., for_update=True)`, which serialises concurrent membership edits against each other (the duplicate-insert race in `test_bug_audit_group_add_members_race.py`) — it does **not** order them against `delete_group`, whose `DELETE` simply waits for it and then cascades the rows away. Declare new advisory-lock ids in `app/core/locks.py`; the single-bigint keyspace already has two uncoordinated occupants. When adding a new periodic task, add a `BackgroundLoop` subclass to the roster in `_build_leader_loop_tasks` (`app/main.py`) and expose Prometheus gauges via `observe_background_loop`, rather than spawning bare `asyncio.create_task` loops. Domain-specific loops live under their owning `app/<domain>/services/` package. Scheduling doctrine: a **BackgroundLoop** per independent lifecycle; **stage_due stages** only as sub-cadences of an owning sweep (host_sweep's partition probe; the janitor's run_reaper/fleet_capacity/pack_drain-backstop/release_rollout/data_cleanup/heartbeat-flush stages, base tick 15 s). Stage cadences are plumbing constants, never registry settings. Pack drain is event-driven: session/run release paths call `complete_drain_if_draining` inline; the janitor stage is the backstop.

### Settings: env vars vs DB registry
There are two distinct config surfaces. Do not conflate them:
- **Process env vars** (`backend/app/core/config.py`, domain config modules such as `backend/app/auth/config.py`, and `agent/agent_app/config.py`) — read once at startup. `GRIDFLEET_DATABASE_URL`, `GRIDFLEET_AUTH_*`, `AGENT_*`, etc.
- **Settings registry** (`app/settings/registry.py`, `app/settings/service.py`) — DB-backed runtime settings editable via the Settings UI. Registry defaults are code constants; env vars never seed registry values. Cross-timer invariants (`app/settings/invariants.py`) are validated on every settings write and at scheduler boot, so the scheduler refuses to start loops on contradictory rows.

See `docs/reference/environment.md` and `docs/reference/settings.md` before adding a new knob.

**Per-domain `BaseSettings` pattern.** Each domain config (`app/<domain>/config.py`) uses `model_config = SettingsConfigDict(env_prefix="", populate_by_name=True, extra="ignore")` with per-field `Field(alias="GRIDFLEET_…")` to preserve ops-facing env-var names. `populate_by_name=True` is **mandatory** — tests construct via field-name kwargs (`AuthConfig(auth_enabled=True)`); without it Pydantic silently drops the kwarg and the test gets the default value.

**Domain config singletons must be reset between tests.** `tests/conftest.py:reset_process_config` snapshots `agent_settings`, `auth_settings`, `packs_settings` and restores at teardown. Adding a new per-domain singleton means extending that fixture; otherwise test mutations leak across the xdist suite (typical symptom: routes return 401 instead of 404 because a stale `auth_enabled=True` persists).

### Driver-pack model (the most important architectural rule)
**Core orchestration must stay driver-agnostic.** Platform-specific behavior — discovery probes, readiness fields, lifecycle actions, capability defaults, health labels — lives in driver-pack manifests under `driver-packs/curated/` and adapter wheels under `driver-packs/adapters/`.

Backend pack pipeline: `app/packs/` (ingest, storage, lifecycle, release, desired-state, dispatch, drain). Agent pack pipeline: `agent/agent_app/pack/` (manifest, runtime, adapter_loader, worker protocol/supervisor, dispatch, state loop, tarball_fetch). The agent pulls a desired pack list from the backend, downloads the verified tarball (sha256-pinned), installs it into an isolated `APPIUM_HOME` runtime under `AGENT_RUNTIME_ROOT`, and starts one stdlib-only JSON-lines worker subprocess per `(pack, release)`. Hook deadlines kill the worker on a hard timeout, keeping a blocking or crashing adapter isolated to its pack; uploaded adapter wheels still execute on agent hosts and remain an untrusted RCE surface.

If you find yourself adding `if pack_id == "appium-uiautomator2"` in core code, stop — push it into the manifest or adapter instead. The agent test `test_no_driver_imports.py` actively guards this for the agent side.

### Host agent lifecycle
1. Agent registers with manager (`AGENT_MANAGER_URL`) on a periodic refresh — enrollment only (identity, IP, agent port, hardware descriptor, plus capabilities as the contract-gate credential); all mutable runtime facts (agent version, capabilities, prerequisites) arrive via the status push, and host online/offline is computed from push recency at read time (`app/hosts/liveness.py`), with the sweep's edge detector emitting `host.status_changed`/`heartbeat_lost` exactly once per transition.
2. Backend signals "start node" → agent allocates an Appium port (`AGENT_APPIUM_PORT_RANGE_*`).
3. Agent spawns `appium` from the runtime venv. There is no Grid relay node; the agent only manages the per-device Appium process.
4. Health checks watch ADB / driver viability and gracefully terminate the Appium process when the device disappears.
5. Push ingest folds the agent's observations into durable facts; `host_sweep` detects silence and `appium_sweep` keeps direct-to-Appium probes for session actions.

Sessions go client → **Rust router on `:4444`** → directly to Appium on the device host; the manager does not proxy WebDriver traffic. The router calls the backend's internal grid API (`/internal/grid/create-session` in `app/grid/router_internal.py`) to claim a device and create the Appium session, then proxies W3C WebDriver commands straight to the allocated Appium. The manager owns reservations, run lifecycle, allocation/queueing, capability matching, and session recording.

### Device row locking
Any code that writes `Device.operational_state_last_emitted` or `Device.lifecycle_policy_state` MUST acquire the row lock first via `app.devices.locking.lock_device` (or `lock_devices` for batch) inside the same transaction. Routers should use `get_device_for_update_or_404` for state-mutating endpoints. The scheduler's singleton advisory lock is NOT sufficient because API mutators run on every worker and bypass it.

`Device.operational_state` is a 5-value enum (`available`, `busy`, `verifying`, `offline`, `maintenance`). Reservation is orthogonal: the computed `is_reserved` flag comes from the `device_reservations` table and is not a column on `Device`. There is no `hold` column.

`operational_state` is a read-time projection from durable facts, using `derive_operational_state` for loaded rows and the SQL twin for query contexts. The intent reconciler's locked edge detector compares that projection with `Device.operational_state_last_emitted`, queues `device.operational_state_changed`, and advances the ledger exactly once per transition. Observation work writes durable facts and does not write the projection. Device-creation paths seed the ledger's initial value. `tests/contracts/test_no_direct_device_state_writes.py` enforces the ledger writer contract. See `docs/reference/device-lifecycle.md`.

For reads, use `operational_state` for SQL filters, counts, presentation, allocation, and composed-state gates. Read the underlying fact for a single-axis question: `in_maintenance(device)`, `device_has_live_session(...)`, or the reservation row. The enum's masking order (`busy > verifying > maintenance > offline`) means a higher state can hide a lower-axis fact.

Writes to `AppiumNode.desired_state` and `AppiumNode.desired_port` MUST go through `app.appium_nodes.services.desired_state_writer.write_desired_state` under the device row lock. `AppiumNode.restart_requested_at` (the restart watermark — "the Appium process must have been spawned at or after T") is also written only by `write_desired_state`; there is no clearing protocol (a satisfied watermark is inert, and a stuck "restarting" projection self-clears at read time after `appium_reconciler.restart_window_sec`). Observation columns (`pid`, `port`, `active_connection_target`, `started_at`, `health_running`, `health_state`, `last_health_checked_at`, `last_observed_at`) are written only by the sanctioned observed-state writers: the `app.appium_nodes.services.reconciler*` modules; `app.devices.services.health.apply_node_state_transition` for health fields; `app.devices.services.capability`'s active-target cache fill; verification flows in `app.verification.services.execution`; and `app.appium_nodes.services.heartbeat`'s `restart_succeeded` event handler. Operator routes and lifecycle flows write desired state only. `PROTECTED_COLUMN_WRITERS` in `tests/contracts/test_no_direct_device_state_writes.py` is the authoritative per-column writer table. Add any new sanctioned writer there in the same change. Alembic schema/data migrations are accepted out-of-band writers; the row-lock contract applies to application code only.

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
- Production compose (`docker-compose.prod.yml`) sets auth on; `agent.auto_accept_hosts` defaults to false (operators approve hosts manually).

## Conventions

- **Migrations:** every schema change needs an Alembic revision in `backend/alembic/versions/`. Run `uv run alembic upgrade head` after pulling changes that touch models.
- **Strict typing:** `mypy --strict` on both backend and agent. Pydantic plugin is enabled.
- **Ruff lint set:** backend selects `E,F,W,I,N,UP,B,A,BLE,SIM,TC,RUF,ANN,FAST,PL` — the full Pylint and FastAPI groups, with `PLW0603` ignored (run-once `global` singleton/guard idioms), `pylint.max-args = 8`, `PLR0913` exempted per-file for routers / `protocols.py` / `agent_comm`, and the complexity/arg PL rules exempted under `tests/**`. `FAST` (FastAPI) requires DI params in `Annotated[T, Query()/Path()/Depends()]` form. The agent uses `E,F,W,I,N,UP,B,A,SIM,TCH,RUF,ANN,PLC0415` (no `BLE`). SQLAlchemy `Mapped[]` columns under domain model packages are exempt from `TCH003` because runtime types are required.
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
- `docs/reference/events.md` — SSE + event names
- `docs/guides/security.md` — threat model and network boundaries
- `docs/runbooks/` — incident response with exact commands
