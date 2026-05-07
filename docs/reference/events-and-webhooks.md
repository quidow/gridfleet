# Events And Webhooks Reference

This page documents the shipped live-event contract used by SSE subscribers, recent-notification polling, and webhook delivery.

> [!IMPORTANT]
> `device.availability_changed` was removed with the device-state split. Subscribe to `device.operational_state_changed` and `device.hold_changed` instead.

## Endpoints

| Method | Path | Purpose | Query/body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/events/catalog` | Read the canonical emitted-event catalog for UI pickers and filters | none | event catalog object |
| `GET` | `/api/events` | Subscribe to live server-sent events | optional `types` and `device_ids` filters | SSE stream |
| `GET` | `/api/notifications` | Read recent in-memory event history | `limit`, optional `types` | recent event array |
| `GET` | `/api/webhooks` | List webhooks | none | `WebhookRead[]` |
| `POST` | `/api/webhooks` | Create a webhook | `WebhookCreate` with valid `event_types` only | `WebhookRead` |
| `GET` | `/api/webhooks/{webhook_id}` | Read a webhook | path `webhook_id` | `WebhookRead` |
| `PATCH` | `/api/webhooks/{webhook_id}` | Update a webhook | `WebhookUpdate` with valid `event_types` only | `WebhookRead` |
| `DELETE` | `/api/webhooks/{webhook_id}` | Delete a webhook | path `webhook_id` | empty `204` |
| `POST` | `/api/webhooks/{webhook_id}/test` | Publish a synthetic test event | path `webhook_id` | status object |

## Event Envelope

The manager publishes one shared event object shape:

```json
{
  "type": "device.operational_state_changed",
  "id": "0d5f0af1-7c2b-4ec4-98c3-90cf7b0d52ef",
  "timestamp": "2026-04-01T12:34:56.789012+00:00",
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
- The event log is in-memory and recent-only; it is not a durable event store

### Webhook delivery shape

- Webhooks receive the same JSON envelope via HTTP `POST`
- Delivery currently retries up to 3 times with exponential backoff (`1s`, `4s`, `16s`)
- Only enabled webhooks whose `event_types` include the published event name receive the event
- Webhook create/update rejects unknown event names with `422`

## Emitted Event Names

### Device and node lifecycle

| Event | Typical `data` fields | Source |
| --- | --- | --- |
| `device.operational_state_changed` | `device_id`, `device_name`, `old_operational_state`, `new_operational_state`, optional `reason` | node lifecycle, health recovery/failure, session-sync busy/idle flows |
| `device.hold_changed` | `device_id`, `device_name`, `old_hold`, `new_hold`, optional `reason` | maintenance and run/reservation flows |
| `device.verification.updated` | full verification job snapshot | verification pipeline |
| `device.hardware_health_changed` | `device_id`, `device_name`, `old_status`, `new_status`, battery telemetry fields | hardware telemetry loop |
| `node.state_changed` | `device_id`, `device_name`, `old_state`, `new_state`, optional `port` | node start/stop/recovery paths |
| `node.crash` | `device_id`, `device_name`, `error`, `will_restart` | node-health failure handling |
| `device.crashed` | `device_id`, `device_name`, `source`, `reason`, `will_restart`, `process` | persisted `node_crash` incidents |
| `config.updated` | `device_id`, `device_name`, `changed_by` | device config writes |

### `device.crashed`

Per-device crash signal. Fires whenever a `DeviceEvent` row of type `node_crash` is persisted. Distinct from `node.crash` (per-Appium-process): `device.crashed` is the device-granularity counterpart and aligns semantically with `device.operational_state_changed` and `device.health_changed`.

**Sources:** `lifecycle_policy_actions.handle_node_crash`, `heartbeat._ingest_appium_restart_events`, and `node_health._process_node_health`.

| Field | Type | Notes |
| --- | --- | --- |
| `device_id` | string (UUID) | Device identifier. |
| `device_name` | string | Display name. |
| `source` | string | One of `appium_crash`, `connectivity_lost`, `health_check_fail`, `agent_restart_exhausted`. |
| `reason` | string | Free-form; mirrors `DeviceEvent.details["reason"]` or crash error text. |
| `will_restart` | bool | Whether lifecycle policy or agent restart logic will retry. |
| `process` | string \| null | `"appium"` or `"grid_relay"` for heartbeat restart events; `null` for probe-driven and lifecycle-driven crashes. |

Dispatched after the writer transaction commits. Dropped on rollback.

### Host and discovery

| Event | Typical `data` fields | Source |
| --- | --- | --- |
| `host.registered` | `host_id`, `hostname`, `status` | host self-registration |
| `host.status_changed` | `host_id`, `hostname`, `old_status`, `new_status` | approval, heartbeat recovery, heartbeat loss |
| `host.heartbeat_lost` | `host_id`, `hostname`, `missed_count` | heartbeat loop |
| `host.discovery_completed` | discovery summary fields for the host | discovery API |

### Sessions and runs

| Event | Typical `data` fields | Source |
| --- | --- | --- |
| `session.started` | `session_id`, `device_id`, `device_name`, optional `test_name`, optional `run_id`, optional requested-lane fields | Grid session sync and direct terminal setup-failure registration |
| `session.ended` | `session_id`, `device_id`, `device_name`, `status`, optional requested-lane fields, optional `error_type`, optional `error_message` | Grid session sync and external terminal status reporting |
| `run.created` | `run_id`, `name`, `device_count`, `created_by` | run creation |
| `run.ready` | `run_id`, `name` | run state transition |
| `run.active` | `run_id`, `name` | run state transition |
| `run.completed` | `run_id`, `name` | run completion |
| `run.cancelled` | `run_id`, `name` | cancel and force-release flows |
| `run.expired` | `run_id`, `name` | run TTL or heartbeat expiration |
| `device.cooldown_escalated` | `device_id`, `device_name`, `cooldown_count`, `threshold`, `reason`, `worker_id`, `run_id`, `run_name` | a device's release-with-cooldown count for a single run reached the configured threshold; the device is moved to maintenance and excluded from the run |

### Groups, bulk actions, settings, and cleanup

| Event | Typical `data` fields | Source |
| --- | --- | --- |
| `device_group.updated` | `group_id`, `action` | group create/update/delete |
| `device_group.members_changed` | `group_id`, `added` or `removed` | static group membership writes |
| `bulk.operation_completed` | `operation`, `total`, `succeeded`, `failed` | device and group bulk actions |
| `settings.changed` | `key` plus `value` or `reset`, `keys`, or `reset_all` | settings writes |
| `system.cleanup_completed` | `sessions_deleted`, `audit_entries_deleted`, `device_events_deleted`, `host_resource_samples_deleted` | retention cleanup loop |
| `webhook.test` | `webhook_id`, `webhook_name`, `message` | webhook test endpoint |

## Event Delivery Semantics

Transactional events (those produced inside code paths that mutate the database) dispatch to webhook and SSE subscribers only after the writer's SQLAlchemy transaction commits successfully. If the transaction rolls back, queued events are dropped, so subscribers do not observe state transitions that did not become durable.

This is rollback-safe but not a durable outbox. Events are queued in memory on `Session.info`; the SQLAlchemy `after_commit` hook schedules `event_bus.publish` with `loop.create_task`, and `event_bus.publish` persists the `SystemEvent` row in a separate transaction. If the process exits between the writer commit and the `SystemEvent` commit, the event can be lost. A durable outbox is out of scope for issue #73.

A small set of broadcasters publish eagerly without an outer transaction: in-memory circuit-breaker transitions, background-loop summaries, synthetic test events, per-device-session bulk summaries, and helpers that open their own short-lived persistence session before publishing. These are listed with rationale in `backend/tests/test_event_bus_publish_allowlist.py`.

Within a single transaction, events queued in source order dispatch in FIFO order. Cross-transaction ordering across event types is not guaranteed; subscribers that need ordering should use the event envelope `timestamp` field set by `app.services.event_bus.Event.to_dict()`. Per-type payloads do not consistently carry their own timestamps.

Run terminal events (`run.completed`, `run.cancelled`, `run.expired`) now dispatch via the `after_commit` hook and can interleave with `_complete_deferred_stops_post_commit`. Subscribers must not assume deferred lifecycle cleanup has finished by the time the run terminal event arrives.

## Persisted Device Event Types

The `device_events` table is narrower than the live event bus. The persisted enum currently contains:

- `health_check_fail`
- `connectivity_lost`
- `node_crash`
- `node_restart`
- `hardware_health_changed`
- `connectivity_restored`
- `lifecycle_deferred_stop`
- `lifecycle_auto_stopped`
- `lifecycle_recovery_suppressed`
- `lifecycle_recovery_failed`
- `lifecycle_recovery_backoff`
- `lifecycle_recovered`
- `lifecycle_run_excluded`
- `lifecycle_run_restored`
- `lifecycle_run_cooldown_escalated`

## Notes

- The current event contract is code-owned and additive; this repo does not yet publish a separate versioned schema for each event payload.
- `notifications.toast_events` is validated and normalized against this emitted-event catalog.
- A device-less setup failure created directly through `POST /api/sessions` emits both `session.started` and `session.ended` so SSE/webhook consumers still observe a lifecycle-shaped sequence.
