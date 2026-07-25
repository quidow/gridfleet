# Events Reference

This page documents the shipped live-event contract used by SSE subscribers and recent-notification polling.

> [!IMPORTANT]
> `device.availability_changed` was removed with the device-state split. Subscribe to `device.operational_state_changed` instead.

## Endpoints

| Method | Path | Purpose | Query/body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/events/catalog` | Read the canonical emitted-event catalog for UI pickers and filters | none | event catalog object |
| `GET` | `/api/events` | Subscribe to live server-sent events | optional `types` and `device_ids` filters | SSE stream |
| `GET` | `/api/notifications` | Read recent persisted `system_events` history (durable; in-memory fallback when persistence is unconfigured) | `limit`, `offset`, optional `types`, optional `severity` | recent event array |

## Severity

Every system event includes a `severity` field at the top level:

- `info` — routine state transition; no action needed.
- `success` — recovery or positive outcome.
- `warning` — operator attention warranted but not urgent.
- `critical` — incident; investigate.
- `neutral` — low-noise bookkeeping (settings, config, test_data updates).

Severity is decided by the backend at the emit site rather than from the event type alone. For `device.operational_state_changed` it derives from the transition itself: any transition to `offline` is `warning`, a recovery to `available` (from any state but `busy`) is `success`, and everything else — including `available → busy` session starts — is `info`.

Old rows persisted before this field existed will have `severity = null`; consumers that need a value should fall back to the catalog's `default_severity` for that event type.

The canonical per-event `default_severity` and `allowed_severities` values are available from `GET /api/events/catalog`. Refer to that endpoint rather than hard-coding per-type severity assumptions in clients.

## Event Envelope

The manager publishes one shared event object shape:

```json
{
  "type": "device.operational_state_changed",
  "id": "0d5f0af1-7c2b-4ec4-98c3-90cf7b0d52ef",
  "timestamp": "2026-04-01T12:34:56.789012+00:00",
  "severity": "info",
  "data": {
    "device_id": "uuid",
    "device_name": "Lab Fire TV",
    "old_operational_state": "offline",
    "new_operational_state": "available"
  }
}
```

### SSE delivery shape

- Event name: the SSE `event:` field is the event `type`
- Event id: the SSE `id:` field is the event `id`
- Event data: the SSE `data:` field contains the full JSON envelope above
- Keepalive: the backend emits comment keepalives roughly every 15 seconds when no events arrive

### Notification polling shape

- `/api/notifications` returns an array of the same event envelopes
- The event log returned by `/api/notifications` comes from the durable `system_events` table (persisted `SystemEvent` rows, ordered newest-first); these rows are pruned by retention cleanup like the other tables the cleanup loop sweeps — rows older than the `retention.system_events_days` setting (by `created_at`; default 30 days) are deleted, or never deleted if that setting is `0`. An in-memory recent-only buffer is used only as a fallback when persistence is not configured

## Emitted Event Names

### Device and node lifecycle

| Event | Typical `data` fields | Default severity | Allowed severities | Source |
| --- | --- | --- | --- | --- |
| `device.operational_state_changed` | `device_id`, `device_name`, `old_operational_state`, `new_operational_state` | `info` | all | node lifecycle, health recovery/failure, session-sync busy/idle flows |
| `device.verification.updated` | full verification job snapshot | `info` | `info`, `success`, `warning`, `critical` | verification pipeline |
| `node.state_changed` | `device_id`, `device_name`, `old_state`, `new_state`, optional `port` | `info` | `info`, `success`, `warning` | node start/stop/recovery paths |
| `device.lifecycle_incident` | `device_id`, `device_name`, `event_type`, `label`, `summary_state`, plus nullable `reason`, `detail`, `source`, `run_id`, `run_name` | `info` | all | lifecycle incident recorder (`lifecycle.services.incidents`): recovery suppressed/failed/backoff/recovered, deferred and auto stop, node-health escalation, run exclusion/cooldown |
| `node.crash` | `device_id`, `device_name`, `error`, `will_restart` | `critical` | `critical`, `warning` | node-health failure handling |
| `device.crashed` | `device_id`, `device_name`, `source`, `reason`, `will_restart`, `process` | `critical` | `critical`, `warning` | persisted `node_crash` incidents |
| `device.health_changed` | `device_id`, `overall`, `device`, `node`, `viability` | `info` | `info`, `success`, `warning` | any health verdict status change |
| `config.updated` | `device_id`, `device_name`, `changed_by` | `neutral` | `neutral` | device config writes |
| `test_data.updated` | `device_id`, `device_name`, `changed_by` | `neutral` | `neutral` | device test_data writes |
> **Breaking change:** the `device.health_changed` payload changed in the next backend major — it was previously `device_id`, `healthy`, `summary`.


### `device.crashed`

Per-device crash signal. Fires whenever a `DeviceEvent` row of type `node_crash` is persisted. Distinct from `node.crash` (per-Appium-process): `device.crashed` is the device-granularity counterpart and aligns semantically with `device.operational_state_changed` and `device.health_changed`.

**Sources:** `lifecycle.services.actions.handle_node_crash` and `heartbeat._ingest_appium_restart_events`.

| Field | Type | Notes |
| --- | --- | --- |
| `device_id` | string (UUID) | Device identifier. |
| `device_name` | string | Display name. |
| `source` | string | Heartbeat-driven crashes use `appium_crash` or `agent_restart_exhausted`. Lifecycle/probe-driven crashes pass the raw lifecycle source string through, e.g. `connectivity`, `session_viability`, or `health_failure:<...>` variants. (Note: `connectivity_lost` / `health_check_fail` are `DeviceEventType` audit-row values, not `device.crashed` source values.) |
| `reason` | string | Free-form; mirrors `DeviceEvent.details["reason"]` or crash error text. |
| `will_restart` | bool | Whether lifecycle policy or agent restart logic will retry. |
| `process` | string \| null | `"appium"` or `"grid_relay"` for heartbeat restart events; `null` for probe-driven and lifecycle-driven crashes. |

Dispatched after the writer transaction commits. Dropped on rollback.

### Host and discovery

| Event | Typical `data` fields | Default severity | Allowed severities | Source |
| --- | --- | --- | --- | --- |
| `host.registered` | `host_id`, `hostname`, `status` | `success` | `success`, `info` | host self-registration |
| `host.status_changed` | `host_id`, `hostname`, `old_status`, `new_status` | `info` | `info`, `success`, `warning`, `critical` | approval, sweep edge detection (recovery and loss) |
| `host.heartbeat_lost` | `host_id`, `hostname`, `stale_for_sec`, `last_push_at` | `critical` | `critical`, `warning` | host sweep |
| `host.discovery_completed` | discovery summary fields for the host | `info` | `info` | discovery API |
| `host.circuit_breaker.opened` | `host`, `consecutive_failures`, `cooldown_seconds`, `last_error` | `critical` | `critical`, `warning` | in-memory circuit-breaker transition |
| `host.circuit_breaker.closed` | `host` | `success` | `success` | in-memory circuit-breaker transition |

### Sessions and runs

| Event | Typical `data` fields | Default severity | Allowed severities | Source |
| --- | --- | --- | --- | --- |
| `session.started` | `session_id`, `device_id`, `device_name`, optional `test_name`, optional `run_id`, optional `requested_capabilities` | `info` | `info` | Session registration at allocation confirm |
| `session.ended` | `session_id`, `device_id`, `device_name`, `status`, optional `requested_capabilities`, optional `error_type`, optional `error_message` | `info` | `info`, `success`, `warning`, `critical` | Observed session end (state change in the direct-to-Appium session-sync sweep or the router's session-end notification) and external terminal status reporting |
| `run.created` | `run_id`, `name`, `device_count`, `created_by` | `info` | `info` | run creation |
| `run.active` | `run_id`, `name` | `info` | `info` | run state transition |
| `run.completed` | `run_id`, `name`, `duration` | `success` | `success`, `warning` | run completion |
| `run.cancelled` | `run_id`, `name` | `warning` | `warning`, `info` | cancel and force-release flows |
| `run.expired` | `run_id`, `name`, `reason` | `critical` | `critical`, `warning` | run TTL or heartbeat expiration |
| `run.never_activated` | `run_id`, `name`, `reason` | `warning` | `warning` | Run hit its TTL / heartbeat budget while still in `preparing` — `/api/runs/{id}/active` was never signaled. Fired immediately before `run.expired`. |

### Groups, bulk actions, settings, and cleanup

| Event | Typical `data` fields | Default severity | Allowed severities | Source |
| --- | --- | --- | --- | --- |
| `device_group.updated` | `group_key`, `action` | `neutral` | `neutral`, `info` | group create/update/delete |
| `device_group.members_changed` | `group_key`, `added`, `removed` | `neutral` | `neutral`, `info` | static group membership writes |
| `bulk.operation_completed` | `operation`, `total`, `succeeded`, `failed` | `success` | `success`, `warning`, `critical` | device and group bulk actions |
| `settings.changed` | `key` plus `value` or `reset`, `keys`, or `reset_all` | `neutral` | `neutral`, `info` | settings writes |
| `system.cleanup_completed` | `sessions_deleted`, `probe_sessions_deleted`, `audit_entries_deleted`, `test_data_audit_entries_deleted`, `device_events_deleted`, `remediation_log_entries_deleted`, `host_resource_samples_deleted`, `capacity_snapshots_deleted`, `grid_queue_tickets_deleted`, `system_events_deleted`, `test_runs_deleted`, `jobs_deleted`, `duration_seconds` | `neutral` | `neutral`, `warning` | retention cleanup loop |

Both `device_group.*` events identify the group by its public `group_key`, never by the internal group UUID. `action` on `device_group.updated` is `created`, `updated`, or `deleted`. `device_group.members_changed` carries `added` **or** `removed` — whichever count the write produced — and is not emitted when a membership write changes nothing (for example re-adding a device that is already a member). Because the key is immutable, a subscriber can use it as a stable correlation id for a group's whole lifetime; a `deleted` action is the only end to that identity.

## Event Delivery Semantics

Transactional events (those produced inside code paths that mutate the database) dispatch to SSE subscribers only after the writer's SQLAlchemy transaction commits successfully. If the transaction rolls back, queued events are dropped, so subscribers do not observe state transitions that did not become durable.

Persistent mode backs this with a transactional outbox on the `system_events` table, not an in-memory queue. `queue_for_session` validates the event through the catalog (`build_event`) and stages a `SystemEvent` row on the caller's own open transaction (`stage_system_event`); the row commits or rolls back with the change that produced it, savepoints included. An `AFTER INSERT` trigger on `system_events` calls `pg_notify('system_events', NEW.id::text)` — Postgres releases that notification only at the outer commit and drops it on any rollback. Every process, including the writer, then reloads the committed row from the database before dispatching it — through the reconnecting `LISTEN`/`NOTIFY` listener, or through the poller, which advances its watermark only past ids that no in-flight transaction can still fill. Delivery is at-least-once with a bounded, process-local dedupe window; handlers must stay idempotent. `EventBus.publish` still exists for standalone effects with no source transaction to ride: it opens one short-lived transaction of its own, stages the row, and commits, then leaves delivery to the same listener/poller path — it never dispatches locally.

The in-memory fallback (an `EventBus()` configured with no session factory) is unchanged and remains explicitly non-durable: events queue in memory on `Session.info`, an `after_commit` hook schedules dispatch on the loop captured at queue time, and an `after_rollback` hook drops them. A process exit loses whatever was queued; nothing is persisted in this mode.

`driver_pack.upload` is deliberately not in the event catalog, but it is validated and staged the same way as any catalogued event. Uncatalogued events are not severity-less: `build_event` resolves a default severity of `"neutral"` for any event type outside the catalog, so `driver_pack.upload` rows are stored with `severity = "neutral"`. Rows written before this default existed keep whatever they already had, including `NULL`; there is no backfill.

A fixed set of standalone summaries publish eagerly, outside any source transaction, because they summarize effects that already committed (or exist only in memory) rather than a still-open mutation: `host.discovery_completed` (`hosts/router.py`), `host.circuit_breaker.opened` and `host.circuit_breaker.closed` (`agent_comm/circuit_breaker.py`), `bulk.operation_completed` — one callsite each for restart, delete, and reconnect (`devices/services/bulk.py`), and `system.cleanup_completed` (`devices/services/data_cleanup.py`). `backend/tests/events/test_event_bus_publish_allowlist.py` guards this surface three ways: an allowlist of new eager `event_bus.publish(` callsites (empty, because production code injects the publisher under names like `publisher`, never the literal `event_bus`), a single sanctioned `SystemEvent(...)` constructor site (`stage_system_event`), and a single sanctioned `pg_notify` reference (the trigger DDL in `app/events/outbox_schema.py`). A direct `SystemEvent` construction or an application-side `pg_notify` call anywhere else fails the guard; the standalone sites above are documented at the callsite, not in the allowlist.

Within a single transaction, events stage in `system_events.id` order and dispatch in that order: the poller scans with `ORDER BY id`, and the listener delivers `NOTIFY` payloads in the order Postgres sent them. Cross-transaction ordering across event types is not guaranteed; subscribers that need ordering should use the event envelope `timestamp` field set by `app.events.event_bus.Event.to_dict()`. Per-type payloads do not consistently carry their own timestamps.

Run terminal events (`run.completed`, `run.cancelled`, `run.expired`) stage in the same transaction as the run's terminal-state write and dispatch through the same outbox path as any other transactional event; the deferred lifecycle cleanup (`complete_deferred_stops_post_commit`) runs in a separate session after that transaction commits. Subscribers must not assume deferred lifecycle cleanup has finished by the time the run terminal event arrives.

## Persisted Device Event Types

The `device_events` table is narrower than the live event bus. Causes are recorded once, at the observation site that knows them; operational-state transitions themselves persist no audit row. The persisted enum currently contains:

- `health_check_fail` — node-health failure-episode edges (onset and elapsed-window verdict) and non-connectivity remediation escalation
- `connectivity_lost` — device disconnect (connectivity sweep), host heartbeat loss (one per device on the host), connectivity remediation escalation
- `node_crash`
- `node_restart`
- `connectivity_restored` — legacy value; no longer emitted (historical rows only)
- `lifecycle_deferred_stop`
- `lifecycle_auto_stopped`
- `lifecycle_recovery_suppressed`
- `lifecycle_recovery_failed`
- `lifecycle_recovery_backoff`
- `lifecycle_recovered`
- `lifecycle_run_excluded`
- `lifecycle_run_restored`
- `lifecycle_run_cooldown_set`
- `lifecycle_run_cooldown_escalated`
- `maintenance_entered` / `maintenance_exited` — written by the maintenance service at enter/exit fact-write time
- `session_started` — legacy value; was never emitted as a persisted row
- `session_ended` — legacy value; no longer emitted (session history lives on the `sessions` table)
- `auto_stopped` — legacy value; no longer emitted (operator stops appear as `desired_state_changed` rows)
- `desired_state_changed`

## Notes

- The current event contract is code-owned and additive; this repo does not yet publish a separate versioned schema for each event payload.
- `notifications.toast_events` is validated and normalized against this emitted-event catalog.
