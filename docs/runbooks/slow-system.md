# Runbook: Slow System

Use this runbook when the UI feels sluggish, sessions queue for longer than expected, or operators report that the manager is "up" but not keeping up.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` and `/metrics` call below requires HTTP Basic auth with the manager's machine credentials. Export the same machine credentials the testkit uses and pass them with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

`/health/live`, `/health/ready`, `/api/health`, and the router's `/healthz` / `/status` endpoints stay open and do not need `-u`.

## 1. Confirm the backend is actually ready

```bash
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s http://localhost:4444/status | python -m json.tool
```

If `/health/ready` is unhealthy, stop here and focus on the failing `checks` entry before investigating device-level symptoms.

## 2. Check metrics for backlog or loop failures

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/metrics | \
  egrep '^(pending_jobs|active_sessions|background_loop_errors_total|background_loop_runs_total|background_loop_overrun_total|http_unhandled_exception_total|agent_calls_total|webhook_deliveries_total)'
```

Prioritize these patterns:

- `background_loop_errors_total{loop_name="..."}` increasing:
  - one control-plane loop is failing repeatedly
- `background_loop_overrun_total{loop_name="..."}` increasing:
  - that loop is taking longer than its interval (e.g. delayed offline/disconnect detection) even though it isn't erroring
- `http_unhandled_exception_total{path,exc_type,pgcode}` increasing:
  - requests are returning 500s; `exc_type`/`pgcode` name the cause (e.g. `pgcode="40P01"` is a Postgres deadlock)
- `pending_jobs` climbing:
  - durable work is backing up
- `agent_calls_total{outcome!="success"}` increasing:
  - the backend is spending time failing remote agent calls (the only non-failure value is `success`; explicit failure values are `circuit_open`, `http_error`, `timeout`, `dns_error`, `connect_error`, `unexpected_error`)

## 3. Inspect container logs for the slow component

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 router
```

Look for:

- repeated background-loop exceptions
- repeated agent timeouts or `AGENT_UNREACHABLE`
- repeated webhook delivery failures
- long stretches of router or Appium-node startup churn

## 4. Check Postgres saturation

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select state, count(*) from pg_stat_activity group by 1 order by 2 desc;"

docker compose --env-file .env -f docker-compose.prod.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "show max_connections;"
```

If most connections are stuck `active` for a long time, inspect the backend logs before raising pool sizes.

The backend also exports its own pool view (per worker) — check it before assuming exhaustion:

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/metrics | \
  egrep '^db_pool_(size|checked_out|overflow)'
```

`db_pool_checked_out` near `db_pool_size + max_overflow` (and `db_pool_overflow` pinned at its max) means the worker is pool-starved; otherwise the contention is row-level, not pool-level.

## 5. Check whether the issue is queue pressure or device scarcity

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/runs | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" 'http://localhost:8000/api/devices?status=available' | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" 'http://localhost:8000/api/devices?status=offline' | python -m json.tool

curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/metrics | \
  egrep '^(gridfleet_grid_eligible_devices|gridfleet_grid_queue_depth|gridfleet_devices_in_cooldown|gridfleet_grid_allocate_queue_wait_seconds|gridfleet_grid_try_allocate_duration_seconds)'
```

Interpretation:

- `gridfleet_grid_eligible_devices` near zero while `gridfleet_grid_queue_depth` rises:
  - genuine device scarcity (often tightened by `gridfleet_devices_in_cooldown` holding devices out of the eligible set)
- `gridfleet_grid_allocate_queue_wait_seconds` tail high but `gridfleet_grid_try_allocate_duration_seconds` low:
  - clients are waiting for capacity, not for a slow allocation path (the allocate long-poll is working as designed)
- many active runs + very few available devices:
  - expected reservation pressure
- many offline devices or hosts:
  - connectivity/agent incident, not just slowness
- healthy fleet + rising queue:
  - focus on router, Appium-node startup, or backend loop failures

## 6. Next actions

- If readiness or metrics point at control-plane loop failure:
  - follow the specific error in backend logs first
- If hosts are falling offline:
  - continue with [agent-not-connecting.md](agent-not-connecting.md)
- If the pain is concentrated on reserved devices:
  - continue with [stuck-devices.md](stuck-devices.md)
- If the change happened immediately after a deploy:
  - continue with [backend-deploy-restart-rollback.md](backend-deploy-restart-rollback.md)
