# Demo Data Guide

GridFleet ships with a `gridfleet_demo` database that holds realistic 90-day fleet data for UI exploration and Playwright e2e tests. This guide covers setup, scenarios, and troubleshooting.

## Purpose

Demo data serves two goals:

1. **UI Exploration** — A complete, stable fleet baseline that lets you explore dashboards, device tables, and workflows without production data or live devices.
2. **Playwright E2E Baseline** — The minimal scenario seeds consistent test data so e2e tests verify UI behavior against predictable state, not fragile mocks.

The seeding system is deterministic: given the same scenario and random seed, you get identical data every time. This makes test runs reproducible.

## Prerequisites

The full compose stack must be running:

```bash
cd docker && docker compose up -d
```

That's it — no host-side `psql`, `createdb`, `uv`, or Python toolchain is required. The script runs the database client inside the `postgres` container and the migration + seeding CLI inside the `backend` container.

### Override container names

If your containers are named differently (e.g., different compose project name), set:

```bash
export GRIDFLEET_POSTGRES_CONTAINER=my-pg
export GRIDFLEET_BACKEND_CONTAINER=my-backend
./scripts/seed_demo.sh
```

By default the script auto-detects the postgres container by image (`postgres`) and the backend container by name pattern (`backend`).

## Quickstart

From the repository root:

```bash
./scripts/seed_demo.sh
```

This:
1. Creates the `gridfleet_demo` database if missing (via `docker exec postgres createdb`).
2. Runs migrations (`alembic upgrade head`) inside the backend container against the demo DB.
3. Seeds the `full_demo` scenario (~1–2 seconds with default settings).

Point the backend container at the demo DB with the toggle script:

```bash
./scripts/demo-mode.sh on
```

## Scenarios

The seeding package provides three pre-built scenarios. Choose based on your needs:

| Scenario | Row Count (approx) | Wall Time | Use Case |
|----------|-------------------|-----------|----------|
| `minimal` | ~10 rows | < 1s | Playwright e2e baseline; lean UI testing. 1 host, 2 devices, 2 runs, and 1 genuinely busy device from the live run. |
| `full_demo` | ~28k rows (incl. ~24k telemetry rows) | ~1–2s | Realistic UI exploration. 4 hosts (one offline), 33 pack-backed devices across Android, iOS/tvOS, and Fire TV families, ~500 test runs over 90 days, live busy/reserved/excluded devices, pack runtime status, Appium nodes, and setup/verification-required cases. |
| `chaos` | ~200 rows | < 1s | Error/warning UI paths. Offline host, maintenance + flapping devices, a genuinely busy stuck device, and failed jobs. |

`full_demo` now intentionally covers these current-fleet cases at once:

- `busy` devices with active sessions
- `reserved` devices waiting on a live run
- `excluded_from_run` devices still attached to an active reservation
- `maintenance` and `offline` devices
- `verification_required` devices
- `setup_required` devices missing pack-required setup fields
- driver-pack catalog, host install/runtime/doctor status, and one intentional driver-version drift
- running/stopped Appium node rows that match states reachable through the node lifecycle flow
- recent device-config audit and host-terminal session history

### Selecting a Scenario

```bash
./scripts/seed_demo.sh minimal      # tiny baseline
./scripts/seed_demo.sh full_demo    # default; full exploration
./scripts/seed_demo.sh chaos        # test error UI paths
```

Extra CLI flags pass straight through — e.g., `--skip-telemetry` trades the host-resource/analytics history for a faster seed:

```bash
./scripts/seed_demo.sh full_demo --skip-telemetry
```

## Guardrail: The `_demo` Database Name

**Safety first:** The seeder only touches databases whose name ends in `_demo`. This prevents accidents.

If your URL doesn't match:

```
error: refusing to seed production_db: database name must end with '_demo'
(or set GRIDFLEET_SEED_ALLOW_ANY_DB=1 to override)
```

**To override** (use with caution):

```bash
GRIDFLEET_SEED_ALLOW_ANY_DB=1 ./scripts/seed_demo.sh
```

## Toggling the Backend Container Between Dev and Demo

When running the full stack via docker compose, use the toggle script to point the backend container at `gridfleet_demo` (or back to the dev `gridfleet`) without editing `docker-compose.yml`:

```bash
./scripts/demo-mode.sh on       # backend → gridfleet_demo
./scripts/demo-mode.sh off      # backend → gridfleet (dev)
./scripts/demo-mode.sh status   # print current GRIDFLEET_DATABASE_URL
```

Both databases live in the same Postgres container, so switching is just a backend-container restart — dev data is preserved on the other side of the toggle.

Typical flow:

```bash
./scripts/seed_demo.sh full_demo      # populate demo DB
./scripts/demo-mode.sh on             # point backend at demo
# ... explore UI ...
./scripts/demo-mode.sh off            # back to dev
```

## Running Against a Live Backend

`scripts/demo-mode.sh on` starts the backend against `gridfleet_demo` with
`GRIDFLEET_FREEZE_BACKGROUND_LOOPS=1`. In frozen mode the backend serves API reads
normally but skips all 14 leader-owned background loops (heartbeat, session
sync, node health, device connectivity, property refresh, hardware telemetry,
host resource telemetry, durable job worker, webhook delivery, run reaper,
data cleanup, session viability, fleet capacity, pack drain). The seeded fleet stays
"healthy" in the dashboard for as long as the container runs — nothing drifts
to offline because there is no real fleet to poll.

`scripts/demo-mode.sh status` prints `background loops: frozen | running`
alongside the mode.

If you need the loops running against the demo DB (e.g., to reproduce a drift
bug or exercise the reaper), unset the variable and restart:

```bash
cd docker && docker compose \
  -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm -e GRIDFLEET_FREEZE_BACKGROUND_LOOPS= backend \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

To reset drifted demo state to the scripted baseline, reseed:

```bash
./scripts/seed_demo.sh          # reseed to reset to known baseline
```

Reseeding wipes and repopulates in ~1–30 seconds (depending on scenario).

## Troubleshooting

### `DatabaseGuardError: refusing to seed...`

The database name must end in `_demo`. Check your URL:

```bash
echo $GRIDFLEET_DATABASE_URL
# Expected: postgresql+asyncpg://... /gridfleet_demo
```

If you intentionally want a different name, set:

```bash
export GRIDFLEET_SEED_ALLOW_ANY_DB=1
./scripts/seed_demo.sh
```

### Unique-Constraint Violations

This happens when you reseed without `--wipe`:

```bash
./scripts/seed_demo.sh       # with --wipe (default)
```

If you need to skip wiping (not recommended):

```bash
cd backend && GRIDFLEET_DATABASE_URL="postgresql+asyncpg://postgres@localhost/gridfleet_demo" \
  uv run python -m app.seeding --scenario full_demo --no-wipe
```

### Schema Skew

If the `gridfleet_demo` schema is stale (e.g., migrations were added since you last seeded):

```bash
# Force migration before seeding
cd backend && GRIDFLEET_DATABASE_URL="postgresql+asyncpg://postgres@localhost/gridfleet_demo" \
  uv run alembic upgrade head

# Then reseed
./scripts/seed_demo.sh
```

### Slow Seeding

The `full_demo` scenario with telemetry (hundreds of HostResourceSample rows per host over 90 days) can take 60–90 seconds. If you're iterating on UI tests:

```bash
./scripts/seed_demo.sh full_demo    # then ctrl+C if too slow, or:

cd backend && GRIDFLEET_DATABASE_URL="postgresql+asyncpg://postgres@localhost/gridfleet_demo" \
  uv run python -m app.seeding --scenario full_demo --skip-telemetry
```

The `--skip-telemetry` flag drops `HostResourceSample` rows, cutting seed time to 10–30 seconds. Dashboards still render (just without sparkline history).

## CLI Reference

For full control, use the Python module directly:

```bash
cd backend && uv run python -m app.seeding --help
```

Example flags:

```bash
cd backend && GRIDFLEET_DATABASE_URL="postgresql+asyncpg://postgres@localhost/gridfleet_demo" \
  uv run python -m app.seeding \
    --scenario full_demo \
    --seed 99 \
    --wipe \
    --skip-telemetry
```

- `--scenario {minimal,full_demo,chaos}` — which dataset to load (default: `full_demo`).
- `--seed N` — random seed for deterministic generation (default: 42).
- `--wipe` — truncate all tables before seeding (default: true).
- `--no-wipe` — skip truncate (useful for incremental seeding, rare).
- `--skip-telemetry` — omit `HostResourceSample` rows (speeds up `full_demo`).
- `--db-url` — database URL (overrides `GRIDFLEET_SEED_DATABASE_URL` or `GRIDFLEET_DATABASE_URL`).
