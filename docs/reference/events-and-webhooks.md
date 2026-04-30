# Events And Webhooks Reference

This page documents the shipped live-event contract used by SSE subscribers, recent-notification polling, and webhook delivery.

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
  "type": "device.availability_changed",
  "id": "0d5f0af1-7c2b-4ec4-98c3-90cf7b0d52ef",
  "timestamp": "2026-04-01T12:34:56.789012+00:00",
  "data": {
    "device_id": "uuid",
    "device_name": "Lab Fire TV",
    "old_availability_status": "offline",
    "new_availability_status": "available"
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
| `device.availability_changed` | `device_id`, `device_name`, `old_availability_status`, `new_availability_status`, optional `reason` | maintenance, availability changes, host-loss/run-end flows |
| `device.verification.updated` | full verification job snapshot | verification pipeline |
| `device.hardware_health_changed` | `device_id`, `device_name`, `old_status`, `new_status`, battery telemetry fields | hardware telemetry loop |
| `node.state_changed` | `device_id`, `device_name`, `old_state`, `new_state`, optional `port` | node start/stop/recovery paths |
| `node.crash` | `device_id`, `device_name`, `error`, `will_restart` | node-health failure handling |
| `config.updated` | `device_id`, `device_name`, `changed_by` | device config writes |

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

### Groups, bulk actions, settings, and cleanup

| Event | Typical `data` fields | Source |
| --- | --- | --- |
| `device_group.updated` | `group_id`, `action` | group create/update/delete |
| `device_group.members_changed` | `group_id`, `added` or `removed` | static group membership writes |
| `bulk.operation_completed` | `operation`, `total`, `succeeded`, `failed` | device and group bulk actions |
| `settings.changed` | `key` plus `value` or `reset`, `keys`, or `reset_all` | settings writes |
| `system.cleanup_completed` | `sessions_deleted`, `audit_entries_deleted`, `device_events_deleted`, `host_resource_samples_deleted` | retention cleanup loop |
| `webhook.test` | `webhook_id`, `webhook_name`, `message` | webhook test endpoint |

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

## Notes

- The current event contract is code-owned and additive; this repo does not yet publish a separate versioned schema for each event payload.
- `notifications.toast_events` is validated and normalized against this emitted-event catalog.
- A device-less setup failure created directly through `POST /api/sessions` emits both `session.started` and `session.ended` so SSE/webhook consumers still observe a lifecycle-shaped sequence.
