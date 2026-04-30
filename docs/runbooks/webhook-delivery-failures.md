# Runbook: Webhook Delivery Failures

Use this runbook when downstream webhook consumers stop receiving events, or the Settings/Webhooks UI shows repeated delivery failures.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` and `/metrics` call below requires HTTP Basic auth with the manager's machine credentials. Export them once and pass with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

`/health/ready` stays open and does not need `-u`.

## 1. Confirm the backend is healthy enough to dispatch

```bash
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/metrics | egrep '^(webhook_deliveries_total|background_loop_errors_total)'
```

If `webhook_delivery` is failing at the loop level, inspect backend logs before retrying deliveries.

## 2. Inspect the webhook and its recent deliveries

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/webhooks | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/webhooks/WEBHOOK_ID | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" 'http://localhost:8000/api/webhooks/WEBHOOK_ID/deliveries?limit=20' | python -m json.tool
```

Focus on:

- `enabled`
- `url`
- `event_types`
- per-delivery `status`
- `attempts`, `last_error`, and `last_http_status`

## 3. Inspect backend logs for the failing target

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend
```

Common patterns:

- DNS / connection refusal:
  - wrong target URL or unavailable consumer
- repeated 4xx:
  - bad target path, authentication, or payload expectations
- repeated 5xx:
  - downstream service outage

## 4. Retry one failed delivery after fixing the target

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/webhooks/WEBHOOK_ID/deliveries/DELIVERY_ID/retry | python -m json.tool
```

## 5. Send a synthetic test event before declaring the incident closed

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/webhooks/WEBHOOK_ID/test | python -m json.tool
```

## 6. Disable a noisy broken webhook if it is flooding logs

```bash
curl -X PATCH -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/webhooks/WEBHOOK_ID \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}' | python -m json.tool
```

Re-enable it only after the target URL and consumer are confirmed healthy again.
