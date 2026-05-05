# Runbook: Stale "Deferred Stop" On The Dashboard

Use this runbook when the **Device Recovery** card on the dashboard pins a device to **Deferred Stop** even though the underlying health failure was resolved long ago and there is no active client session against the device.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` call below requires HTTP Basic auth with the manager's machine credentials. Export them once and pass with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

## Cause

`Device.lifecycle_policy_state.stop_pending` was set to `true` by `lifecycle_policy.handle_health_failure` when a health probe failed during a running client session. Historically only the Selenium Grid hub disappearance path cleared the flag. Other session-end paths (PATCH `/api/sessions/{id}/status`, register-with-terminal-status, run release) left the flag set, so the dashboard kept rendering the device as "affected".

## Automatic recovery

As of the fix in this release:

- Each session-end path (`PATCH /api/sessions/{id}/status`, `register_session` with a terminal status, run release / cancel / force-release / expire, and `session_sync` end-of-session) calls `lifecycle_policy.complete_deferred_stop_if_session_ended`. The helper delegates to `handle_session_finished`, which re-reads the device under a row lock so a concurrent fresh session cannot be raced past.
- `update_session_status` runs the helper on terminal status updates regardless of current device availability — `maintenance` and `offline` rows with stale `stop_pending` are healed too, not just `busy`.
- The `session_sync` background loop runs `_sweep_stale_stop_pending` every cycle as a backstop. The sweep relies on DB state only and runs **independent of Grid availability** — historical stale rows are healed within one poll interval even when the Grid hub is unreachable. The poll interval is governed by the `grid.session_poll_interval_sec` setting.
- When health recovers before the session ends (e.g. via `node_health` recovery), the deferred-stop intent is cleared in place and the device stays up — the audit trail records a single `lifecycle_recovered` event, and `last_action` advances to `auto_stop_cleared` so the dashboard does not show a stale `auto_stop_deferred`.
- The session-end helper trusts the typed health columns plus `AppiumNode` row as the canonical health source. If the derived summary reads healthy but `last_failure_*` still describes a recent failure, the intent is cleared rather than auto-stopping a device the row says is working. A subsequent failed probe will re-arm the deferred stop.

If the dashboard still shows a stale "Deferred Stop" entry, the periodic sweep should clear it within one poll interval. If it does not, follow the manual recovery below.

## 1. Inspect the device record first

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/devices/DEVICE_ID | python -m json.tool
```

Focus on:

- `operational_state`
- `hold`
- `lifecycle_policy_summary.state` — should be `deferred_stop` for this scenario
- `lifecycle_policy_state.stop_pending`, `stop_pending_reason`, `stop_pending_since`
- recent `events` for the device

Confirm there is no running session against the device:

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" "http://localhost:8000/api/devices/DEVICE_ID/sessions?limit=5" | python -m json.tool
```

If a session is still in `running` status with no `ended_at`, do NOT proceed — fix the session first using `docs/runbooks/stuck-devices.md`.

## 2. Manual recovery (immediate, single device)

If the periodic sweep has not yet caught the row and you cannot wait for the next cycle, run this against the manager Postgres:

```sql
UPDATE devices
SET lifecycle_policy_state = jsonb_set(
      jsonb_set(
        jsonb_set(lifecycle_policy_state::jsonb, '{stop_pending}', 'false'::jsonb),
        '{stop_pending_reason}', 'null'::jsonb),
      '{stop_pending_since}', 'null'::jsonb)::json
WHERE id = 'DEVICE_UUID'
  AND (lifecycle_policy_state->>'stop_pending')::bool;
```

The column is a Postgres `JSON` (not `JSONB`) — the explicit `::jsonb` and `::json` casts are required so `jsonb_set` operates on the right type and the result fits back into the column. The `WHERE` guard makes the statement a no-op when the row is already clean.

If multiple devices are affected, drop the `id` filter; the guard prevents collateral damage.

## 3. Verify

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/devices/DEVICE_ID | python -m json.tool | grep -A2 lifecycle_policy_summary
```

Expected: `state` is no longer `deferred_stop`. The dashboard refreshes within one `useDevices` poll (5–15 s by default).
