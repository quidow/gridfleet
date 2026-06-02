# Device diagnostic export

The diagnostic export captures a coherent JSON snapshot of a device's orchestration state on operator demand and automatically when a device crosses into `review_required`. Operators can attach it to an investigation without privileged database access.

## Endpoints

- `POST /api/diagnostics/devices/{device_id}/export?persist=true&redact=false`
- `GET /api/diagnostics/devices/{device_id}/snapshots?limit=20&before=<id>`
- `GET /api/diagnostics/devices/{device_id}/snapshots/{snapshot_id}?redact=false`

All three live behind the standard `/api/devices/*` auth wall. The POST is rate-limited to one capture per device per 5 seconds; throttled calls return 429 with `Retry-After`.

## Bundle shape

Every bundle is a JSON object with these top-level keys:

- `schema_version`: integer. Bumped only on breaking shape changes.
- `captured_at`: ISO-8601 UTC timestamp of assembly.
- `redacted`: true when sensitive identifiers have been hashed on read.
- `device`: projection of the device row at capture.
- `appium_node`: node row or `null`.
- `reservations`: every reservation row, most-recent first.
- `intents`: current intents, capped at 200. Truncation is surfaced in `warnings`.
- `sessions.running`: live sessions for this device.
- `sessions.recent_ended`: last 20 ended sessions by `ended_at desc`.
- `events`: last 50 events by `created_at desc`.
- `related_runs`: every `TestRun` referenced by the rows above.
- `agent_reconfigure_outbox`: pending rows plus the last 5 delivered or abandoned rows.

## Redaction

When `redact=true`, the following fields are hashed using a per-deployment salt:

- `device.identity_value`, `device.connection_target`, `device.ip_address`
- `appium_node.active_connection_target`
- `reservations[].identity_value`, `reservations[].connection_target`, `reservations[].host_ip`
- `sessions.*.session_id`
- `related_runs[].name`
- `events[].details`, recursively

Hash format: `redacted:<8-hex>`. Hashes are deterministic within a single deployment and unlinkable across deployments. The salt is not exposed through the settings registry; operators cannot rotate or read it from the UI. To rotate it, delete the row in `control_plane_state_entries` under namespace `diagnostic_redaction`.

## Retention

Captured snapshots are deleted by the existing `data_cleanup` background loop using `retention.diagnostic_snapshots_days` (default 30).

## Curl recipe

```bash
curl -u <user>:<pass> \
  -X POST "https://<host>/api/diagnostics/devices/<device_id>/export?redact=true" \
  -o snapshot.json
```
