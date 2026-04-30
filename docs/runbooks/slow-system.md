# Runbook: Slow System

Use this runbook when the UI feels sluggish, sessions queue for longer than expected, or operators report that the manager is "up" but not keeping up.

## 1. Confirm the backend is actually ready

```bash
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s http://localhost:4444/status | python -m json.tool
```

If `/health/ready` is unhealthy, stop here and focus on the failing `checks` entry before investigating device-level symptoms.

## 2. Check metrics for backlog or loop failures

```bash
curl -s http://localhost:8000/metrics | \
  egrep '^(pending_jobs|active_sessions|background_loop_errors_total|background_loop_runs_total|agent_calls_total|webhook_deliveries_total)'
```

Prioritize these patterns:

- `background_loop_errors_total{loop_name="..."}` increasing:
  - one control-plane loop is failing repeatedly
- `pending_jobs` climbing:
  - durable work is backing up
- `agent_calls_total{outcome="error"}` increasing:
  - the backend is spending time failing remote agent calls

## 3. Inspect container logs for the slow component

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 selenium-hub
```

Look for:

- repeated background-loop exceptions
- repeated agent timeouts or `AGENT_UNREACHABLE`
- repeated webhook delivery failures
- long stretches of Grid startup or node-registration churn

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

## 5. Check whether the issue is queue pressure or device scarcity

```bash
curl -s http://localhost:8000/api/runs | python -m json.tool
curl -s 'http://localhost:8000/api/devices?status=available' | python -m json.tool
curl -s 'http://localhost:8000/api/devices?status=offline' | python -m json.tool
```

Interpretation:

- many active runs + very few available devices:
  - expected reservation pressure
- many offline devices or hosts:
  - connectivity/agent incident, not just slowness
- healthy fleet + rising queue:
  - focus on Grid, node startup, or backend loop failures

## 6. Next actions

- If readiness or metrics point at control-plane loop failure:
  - follow the specific error in backend logs first
- If hosts are falling offline:
  - continue with [agent-not-connecting.md](agent-not-connecting.md)
- If the pain is concentrated on reserved devices:
  - continue with [stuck-devices.md](stuck-devices.md)
- If the change happened immediately after a deploy:
  - continue with [backend-deploy-restart-rollback.md](backend-deploy-restart-rollback.md)
