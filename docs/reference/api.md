# API Reference

This page describes the shipped `/api` surface at a lookup level. It is grouped by domain and focuses on the current contract: method, path, purpose, primary inputs, and primary response shape.

> [!TIP]
> The backend automatically serves an interactive Swagger UI. When running locally, you can explore and test the entire API schema at `http://localhost:8000/docs`. When the login gate is enabled, `/docs`, `/redoc`, `/openapi.json`, and `/metrics` are protected.

Unless noted otherwise, path parameters are UUIDs where the route name implies an entity ID. Validation failures return `422`.

## System

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/health/live` | Liveness probe for the backend process | none | `{"status":"ok"}` |
| `GET` | `/health/ready` | Cluster-aware readiness probe (DB + shared control-plane leader loop heartbeats) | none | object with readiness status and checks |
| `GET` | `/api/health` | Readiness alias for `/health/ready` | none | object with readiness status and checks |
| `GET` | `/metrics` | Prometheus scrape endpoint | none | Prometheus text payload |
| `GET` | `/api/availability` | Check whether enough ready devices exist for a platform | `platform_id`, `count` | object with `available`, `requested`, `matched`, `platform_id` |

## Authentication

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/auth/session` | Read auth-gate state plus the current browser session status | none | `{ enabled, authenticated, username, csrf_token, expires_at }` |
| `POST` | `/api/auth/login` | Create a browser session cookie for the shared operator account | `{ username, password }` | `{ enabled, authenticated, username, csrf_token, expires_at }` |
| `POST` | `/api/auth/logout` | Clear the current browser session cookie | none | `{ enabled, authenticated, username, csrf_token, expires_at }` |

Current auth behavior:

- `GET /api/auth/session` always returns `200`. When auth is disabled it returns `enabled=false`.
- When `GRIDFLEET_AUTH_ENABLED=true`, protected routes accept either a valid browser session cookie or `Authorization: Basic ...` using the machine credential pair.
- Browser-session `POST`, `PUT`, `PATCH`, and `DELETE` requests must include `X-CSRF-Token`.
- Machine-auth requests do not need a CSRF header.
- `/health/live`, `/health/ready`, and `/api/health` stay open even when auth is enabled.
- `/api/*`, `/agent/*`, `/metrics`, `/docs`, `/redoc`, and `/openapi.json` are protected when auth is enabled.

## Devices

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/devices` | List devices with readiness, reservation, lifecycle, and hardware telemetry summary data | filters: `platform_id`, `status`, `host_id`, `identity_value`, `connection_target`, `device_type`, `connection_type`, `os_version`, `search`, `hardware_health_status`, `hardware_telemetry_state`, `needs_attention`, `tags.<key>` | `DeviceRead[]` |
| `GET` | `/api/devices/{device_id}` | Get full device detail | path `device_id` | `DeviceDetail` |
| `PATCH` | `/api/devices/{device_id}` | Apply generic device edits | `DevicePatch` | `DeviceRead` |
| `DELETE` | `/api/devices/{device_id}` | Delete a device | path `device_id` | empty `204` |
| `POST` | `/api/devices/verification-jobs` | Start add-device verification | `DeviceVerificationCreate` | `DeviceVerificationJobRead` (`202`) |
| `POST` | `/api/devices/{device_id}/verification-jobs` | Start re-verification for an existing device | `DeviceVerificationUpdate` | `DeviceVerificationJobRead` (`202`) |
| `GET` | `/api/devices/verification-jobs/{job_id}` | Read verification job state | path `job_id` | `DeviceVerificationJobRead` |
| `GET` | `/api/devices/verification-jobs/{job_id}/events` | Subscribe to verification job SSE updates | path `job_id` | SSE stream |
| `GET` | `/api/devices/{device_id}/capabilities` | Read generated Appium capabilities | path `device_id` | object of Appium capabilities |
| `POST` | `/api/devices/{device_id}/maintenance` | Enter maintenance (stops the node immediately) | `DeviceMaintenanceUpdate` | `DeviceRead` |
| `POST` | `/api/devices/{device_id}/maintenance/exit` | Exit maintenance | path `device_id` | `DeviceRead` |
| `GET` | `/api/devices/{device_id}/sessions` | List recent sessions for one device | `limit` | `SessionRead[]` |
| `GET` | `/api/devices/{device_id}/config` | Read device config, optionally filtered by key | `keys` | config object |
| `PUT` | `/api/devices/{device_id}/config` | Replace device config | config object body | config object |
| `PATCH` | `/api/devices/{device_id}/config` | Deep-merge config keys | partial config object | config object |
| `GET` | `/api/devices/{device_id}/config/history` | Read config audit history | `limit` | config-audit entries |
| `GET` | `/api/devices/{device_id}/health` | Probe current device health through the assigned host | path `device_id` | health/status object |
| `POST` | `/api/devices/{device_id}/lifecycle/{action}` | Run a pack-defined device lifecycle action | action args object | lifecycle action result |
| `GET` | `/api/devices/{device_id}/logs` | Read device/agent log view | `lines` | log payload |
| `POST` | `/api/devices/{device_id}/node/start` | Start the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/node/stop` | Stop the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/node/restart` | Restart the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/reconnect` | Reconnect a network Android/Fire TV transport | path `device_id` | reconnect result |
| `POST` | `/api/devices/{device_id}/refresh` | Refresh device properties from the host | path `device_id` | `DeviceRead` |
| `GET` | `/api/devices/{device_id}/session-outcome-heatmap` | Read recent session outcome points | `days` | `SessionOutcomeHeatmapRow[]` |
| `POST` | `/api/devices/{device_id}/session-test` | Run a session viability probe | path `device_id` | session-test result |

## Bulk Device Actions

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/devices/bulk/start-nodes` | Start nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/stop-nodes` | Stop nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/restart-nodes` | Restart nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/set-auto-manage` | Toggle `auto_manage` in bulk | `BulkAutoManageUpdate` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/update-tags` | Merge or replace tags in bulk | `BulkTagsUpdate` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/delete` | Delete many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/enter-maintenance` | Enter maintenance in bulk | `BulkMaintenanceEnter` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/exit-maintenance` | Exit maintenance in bulk | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/reconnect` | Reconnect eligible network Android devices in bulk | `BulkDeviceIds` | `BulkOperationResult` |

## Hosts

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/hosts/register` | Host self-registration path used by the agent | `HostRegister` | `HostRead` |
| `POST` | `/api/hosts` | Create a host manually | `HostCreate` | `HostRead` |
| `GET` | `/api/hosts` | List hosts | none | `HostRead[]` |
| `GET` | `/api/hosts/{host_id}` | Read host detail including current devices | path `host_id` | `HostDetail` |
| `GET` | `/api/hosts/{host_id}/diagnostics` | Read backend-owned host diagnostics (breaker state, latest Appium process snapshot, recent agent-local Appium recovery events) | path `host_id` | `HostDiagnosticsRead` |
| `GET` | `/api/hosts/{host_id}/resource-telemetry` | Read recent host CPU, memory, and disk telemetry using backend bucketing | `since`, `until`, `bucket_minutes` | `HostResourceTelemetryResponse` |
| `GET` | `/api/hosts/{host_id}/tools/status` | Read host agent Appium, Node, Node provider, and iOS helper versions | path `host_id` | tool status object |
| `POST` | `/api/hosts/{host_id}/tools/ensure` | Start a background job to ensure configured host tool target versions | path `host_id` | `HostToolEnsureJobRead` |
| `GET` | `/api/hosts/{host_id}/tools/ensure-jobs/{job_id}` | Read host tool ensure job status/result | path `host_id`, `job_id` | `HostToolEnsureJobRead` |
| `DELETE` | `/api/hosts/{host_id}` | Delete an empty host | path `host_id` | empty `204` |
| `POST` | `/api/hosts/{host_id}/approve` | Approve a pending host | path `host_id` | `HostRead` |
| `POST` | `/api/hosts/{host_id}/reject` | Reject a pending host | path `host_id` | empty `204` |
| `POST` | `/api/hosts/{host_id}/discover` | Run host-scoped discovery | path `host_id` | `DiscoveryResult` |
| `GET` | `/api/hosts/{host_id}/intake-candidates` | Read discovery-like intake candidates without import | path `host_id` | `IntakeCandidateRead[]` |
| `POST` | `/api/hosts/{host_id}/discover/confirm` | Import/remove discovered devices | `DiscoveryConfirm` | `DiscoveryConfirmResult` |

`HostRead` includes the following version-awareness fields:

- `required_agent_version`: minimum supported agent version, or `null` when the version check is disabled.
- `recommended_agent_version`: manager-recommended agent version, or `null` when no recommendation is configured.
- `agent_version_status`: manager-computed compliance status (`disabled`, `ok`, `outdated`, `unknown`) based on the minimum version policy.
- `agent_update_available`: `true` when the installed agent version is below the recommended version (independent of minimum compliance). Backend-computed using version ordering; clients should not re-implement version comparison.

`GET /api/hosts/{host_id}/resource-telemetry` returns:

- `samples`: bucketed time-series rows with `timestamp`, `cpu_percent`, `memory_used_mb`, `memory_total_mb`, `disk_used_gb`, `disk_total_gb`, and `disk_percent`
- `latest_recorded_at`: the newest stored sample for that host, even if it is outside the requested window
- `window_start`, `window_end`, and `bucket_minutes`: the effective server-side query window and bucket size

Current validation rules:

- `since` must be earlier than `until`
- `bucket_minutes` must stay within `1..1440`
- the requested window cannot exceed `retention.host_resource_telemetry_hours`

## Agent Local API

The agent exposes a local `/agent/health` endpoint. The response includes a `version_guidance` object with fields cached from the latest successful manager registration:

- `version_guidance.required_agent_version`: minimum supported agent version from the last successful manager registration.
- `version_guidance.recommended_agent_version`: recommended agent version from the last successful manager registration.
- `version_guidance.agent_version_status`: manager-computed status for the installed agent version (compared against minimum, not recommended).
- `version_guidance.agent_update_available`: `true` when the installed version trails the recommended version.

## Agent Driver-Pack State

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/agent/driver-packs/desired` | Agent fetches desired driver-pack runtime state | `host_id` | desired pack/runtime payload |
| `POST` | `/agent/driver-packs/status` | Agent reports installed runtimes, pack status, doctor checks, and sidecars | status payload | empty `204` |

## Runs

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/runs` | Create a reservation run; add `?include=config` to inline config per device | `RunCreate` | `RunCreateResponse` (`201`) |
| `GET` | `/api/runs` | List runs | filters: `state`, `created_from`, `created_to`, `limit`, `offset`, `sort_by`, `sort_dir` | `{ items: RunRead[], total, limit, offset }` |
| `GET` | `/api/runs/{run_id}` | Read full run detail | path `run_id` | `RunDetail` |
| `POST` | `/api/runs/{run_id}/ready` | Transition run to `ready` | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/active` | Transition run to `active` | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/claim` | Atomically claim one unclaimed active reservation for a CI worker; add `?include=config,capabilities` to inline config and live Appium capabilities | optional `ClaimRequest` | `ClaimResponse` |
| `POST` | `/api/runs/{run_id}/release` | Release one claimed reservation back to the run's unclaimed pool | `ReleaseRequest` | `{ status: "released" }` |
| `POST` | `/api/runs/{run_id}/devices/{device_id}/release-with-cooldown` | Release one worker claim and cool that reservation down inside the same run | `ReleaseWithCooldownRequest` | `ReleaseWithCooldownResponse` |
| `POST` | `/api/runs/{run_id}/devices/{device_id}/preparation-failed` | Exclude one reserved device after CI preparation failure, persist the exact failure message, and mark the device unhealthy/offline | `RunPreparationFailureReport` | `RunRead` |
| `POST` | `/api/runs/{run_id}/heartbeat` | Refresh heartbeat and read current state | path `run_id` | `HeartbeatResponse` |
| `POST` | `/api/runs/{run_id}/complete` | Complete a run and release devices | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/cancel` | Cancel a run and release devices | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/force-release` | Force release reserved devices | path `run_id` | `RunRead` |

`RunPreparationFailureReport` currently accepts:

- `message`: required string, stored as the exact exclusion and health-summary reason
- `source`: optional string, default `ci_preparation`

Current shipped behavior for `POST /api/runs/{run_id}/devices/{device_id}/preparation-failed`:

- the device must still be actively reserved by that run
- the device is excluded from the run rather than releasing the whole run
- the device transitions to `offline` and unhealthy
- healthy reserved siblings remain attached to the run
- invalid run/device state currently returns `409`

`POST /api/runs/{run_id}/claim` accepts an optional request body:

```json
{ "worker_id": "gw0" }
```

If `worker_id` is omitted, the manager generates an anonymous claim owner. The response is the claimed device info plus:

- `claimed_by`: the supplied or generated claim owner
- `claimed_at`: ISO timestamp for the claim lease

The claim operation is database-atomic for concurrent workers. It returns `404` when the run is missing and `409` when the run is terminal or no unclaimed, non-excluded reserved device is available. Stale claims are expired lazily according to `reservations.claim_ttl_seconds`.

When no device is claimable, `409` responses include a `Retry-After` header and structured details for testkit clients:

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "No unclaimed devices available in this run",
    "details": {
      "error": "no_claimable_devices",
      "retry_after_sec": 5,
      "next_available_at": "2026-05-03T20:00:00Z"
    }
  }
}
```

`next_available_at` is `null` when the server cannot compute a cooldown expiry.

`POST /api/runs` supports an optional `?include=` query parameter:

- `include=config` — inlines the Appium configuration for each reserved device in the response. Useful when the CI orchestrator wants device-level config without a follow-up request.
- `include=capabilities` is rejected with `422` (`details.code = "reserve_capabilities_unsupported"`) because live Appium capabilities are only available after a device is claimed and a session is established.

`POST /api/runs/{run_id}/claim` supports `?include=` to embed extra data in the `ClaimResponse`:

- `include=config` — inlines the Appium configuration for the claimed device.
- `include=capabilities` — inlines the live Appium capabilities reported by the agent for the claimed device.
- Both can be combined: `?include=config,capabilities`.
- Unknown include values return `422` with `details.code = "unknown_include"`.

`POST /api/runs/{run_id}/release` accepts:

```json
{
  "device_id": "reserved-device-uuid",
  "worker_id": "gw0"
}
```

Release is owner-checked: `worker_id` must match the active claim owner. Wrong owner, unclaimed device, and device-not-in-run conditions return `409`; malformed `device_id` returns `422`.

`POST /api/runs/{run_id}/devices/{device_id}/release-with-cooldown` accepts:

```json
{
  "worker_id": "gw0",
  "reason": "appium launch timeout",
  "ttl_seconds": 60
}
```

Release with cooldown is also owner-checked. It clears `claimed_by` / `claimed_at` and increments `cooldown_count` on the reservation row. The response is a discriminated union on `status`:

- `"cooldown_set"` (default) — sets `excluded_until = now + ttl_seconds`. The same run can reclaim the reservation after the TTL expires. Cooldowns are run-scoped in v1: completing or cancelling the run releases the physical device normally and does not quarantine it across future runs.
- `"maintenance_escalated"` — fired when `cooldown_count` reaches `general.device_cooldown_escalation_threshold` (default `3`, set to `0` to disable). The reservation is permanently excluded (`excluded_until = null`), the device is moved to maintenance, and the response includes `cooldown_count` and `threshold` instead of `excluded_until`.

## Sessions

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/sessions` | List recorded Appium sessions | filters: `device_id`, `status`, `platform`, `started_after`, `started_before`, `limit`, `offset`, `sort_by`, `sort_dir` | `{ items: SessionDetail[], total, limit, offset }` |
| `GET` | `/api/sessions/{session_id}` | Read one recorded session | path `session_id` | `SessionDetail` |
| `POST` | `/api/sessions` | Create or register a tracked session attempt, including device-less setup failures | `SessionCreate` | `SessionRead` |
| `PATCH` | `/api/sessions/{session_id}/status` | Write final session status from an external test harness | `SessionStatusUpdate` | `SessionRead` |
| `POST` | `/api/sessions/{session_id}/finished` | Push-notify session end from testkit `driver.quit`; stamps `ended_at` without touching `status`; idempotent | path `session_id` (WebDriver token string) | 204 No Content |

`SessionCreate` now accepts optional setup-attempt fields:

- `status`
- `requested_pack_id`
- `requested_platform_id`
- `requested_device_type`
- `requested_connection_type`
- `requested_capabilities`
- `error_type`
- `error_message`

`SessionRead` / `SessionDetail` now return the same setup-attempt fields alongside the existing session metadata. `requested_capabilities` is validated with a 32 KB serialized size limit.

Run requirements use driver-pack platform identity:

```json
{
  "requirements": [
    { "pack_id": "appium-uiautomator2", "platform_id": "firetv_real", "count": 3 }
  ]
}
```

For CI jobs that should consume the currently available matching fleet slice, use explicit all-available allocation instead of `count`:

```json
{
  "requirements": [
    {
      "pack_id": "appium-uiautomator2",
      "platform_id": "firetv_real",
      "allocation": "all_available",
      "min_count": 1
    }
  ]
}
```

`count` and `allocation: "all_available"` are mutually exclusive. All-available allocation is a snapshot at run creation time; it reserves every currently eligible matching device and returns `409` if fewer than `min_count` devices are eligible.

`connection_type` values now distinguish three lanes:

- `usb` for physical USB-connected devices
- `network` for physical network endpoint devices that require IP-based setup
- `virtual` for emulators and simulators

## Settings

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/settings` | List all settings grouped for the UI | none | `SettingsGrouped[]` |
| `GET` | `/api/settings/{key}` | Read one setting | path `key` | `SettingRead` |
| `PUT` | `/api/settings/{key}` | Update one setting | `SettingUpdate` | `SettingRead` |
| `PUT` | `/api/settings/bulk` | Update many settings in one request | `SettingsBulkUpdate` | `SettingRead[]` |
| `POST` | `/api/settings/reset/{key}` | Reset one setting to its default | path `key` | `SettingRead` |
| `POST` | `/api/settings/reset-all` | Reset every setting to defaults | none | status object |

## Events And Webhooks

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/events/catalog` | Read the canonical emitted-event catalog | none | event catalog object |
| `GET` | `/api/events` | Subscribe to live SSE events | optional `types`, `device_ids` | SSE stream |
| `GET` | `/api/notifications` | Read recent notification history in newest-first order | `limit`, `offset`, optional `types` | `{ items: SystemEventRead[], total, limit, offset }` |
| `GET` | `/api/webhooks` | List configured webhooks | none | `WebhookRead[]` |
| `POST` | `/api/webhooks` | Create a webhook | `WebhookCreate` with valid `event_types` | `WebhookRead` |
| `GET` | `/api/webhooks/{webhook_id}` | Read one webhook | path `webhook_id` | `WebhookRead` |
| `PATCH` | `/api/webhooks/{webhook_id}` | Update a webhook | `WebhookUpdate` with valid `event_types` | `WebhookRead` |
| `DELETE` | `/api/webhooks/{webhook_id}` | Delete a webhook | path `webhook_id` | empty `204` |
| `POST` | `/api/webhooks/{webhook_id}/test` | Publish a synthetic `webhook.test` event | path `webhook_id` | status object |

`DeviceRead` / `DeviceDetail` expose device state as `operational_state` (`available`, `busy`, `offline`) plus nullable `hold` (`maintenance`, `reserved`, or `null`). UI clients derive the legacy status chip from `hold ?? operational_state`.

They also return the latest hardware telemetry snapshot fields:

- `battery_level_percent`
- `battery_temperature_c`
- `charging_state`
- `hardware_health_status`
- `hardware_telemetry_reported_at`
- `hardware_telemetry_state`

## Device Groups

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/device-groups` | List static and dynamic groups | none | `DeviceGroupRead[]` |
| `POST` | `/api/device-groups` | Create a group | `DeviceGroupCreate` | `DeviceGroupRead` (`201`) |
| `GET` | `/api/device-groups/{group_id}` | Read group detail and resolved devices | path `group_id` | `DeviceGroupDetail` |
| `PATCH` | `/api/device-groups/{group_id}` | Update group metadata or filter rules | `DeviceGroupUpdate` | `DeviceGroupRead` |
| `DELETE` | `/api/device-groups/{group_id}` | Delete a group | path `group_id` | empty `204` |
| `POST` | `/api/device-groups/{group_id}/members` | Add static-group members | `GroupMembershipUpdate` | added-count object |
| `DELETE` | `/api/device-groups/{group_id}/members` | Remove static-group members | `GroupMembershipUpdate` | removed-count object |
| `POST` | `/api/device-groups/{group_id}/bulk/start-nodes` | Run group-scoped bulk start | none | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/stop-nodes` | Run group-scoped bulk stop | none | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/restart-nodes` | Run group-scoped bulk restart | none | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/enter-maintenance` | Run group-scoped maintenance enter | `BulkMaintenanceEnter` | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/exit-maintenance` | Run group-scoped maintenance exit | none | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/reconnect` | Run group-scoped reconnect | none | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/update-tags` | Run group-scoped tag update | `BulkTagsUpdate` | `BulkOperationResult` |
| `POST` | `/api/device-groups/{group_id}/bulk/delete` | Run group-scoped delete | none | `BulkOperationResult` |

Dynamic group request bodies now use `filters`, not `filter_rules`.

Supported dynamic group filters:

- `platform`
- `status`
- `host_id`
- `identity_value`
- `connection_target`
- `device_type`
- `connection_type`
- `os_version`
- `hardware_health_status`
- `hardware_telemetry_state`
- `needs_attention`
- `tags` as a key/value object

## Grid, Analytics, And Lifecycle

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/grid/status` | Read Grid status plus registry/device summary | none | grid/registry summary object |
| `GET` | `/api/grid/queue` | Read queued Grid session requests | none | queue summary object |
| `GET` | `/api/analytics/sessions/summary` | Read aggregated session analytics | `date_from`, `date_to`, `group_by`, `format` | `SessionSummaryRow[]` |
| `GET` | `/api/analytics/devices/utilization` | Read device utilization analytics | `date_from`, `date_to`, `format` | `DeviceUtilizationRow[]` |
| `GET` | `/api/analytics/devices/reliability` | Read device reliability analytics | `date_from`, `date_to`, `format` | `DeviceReliabilityRow[]` |
| `GET` | `/api/analytics/fleet/overview` | Read aggregate fleet analytics | `date_from`, `date_to` | `FleetOverview` |
| `GET` | `/api/lifecycle/incidents` | Read recent lifecycle incident history | `limit`, optional `device_id` | `LifecycleIncidentRead[]` |

## Notes

- The API contract is currently owned by the backend code and FastAPI schemas, not by a separate versioned OpenAPI publishing pipeline.
- Backend responses include `X-Request-ID` so operators can correlate logs and backend-to-agent calls.
- `/api/events` and webhook delivery share the same event envelope. See [events-and-webhooks.md](events-and-webhooks.md) for the emitted event names.
- The current product keeps add-device and re-verification flows behind verification-job routes instead of exposing a general-purpose `POST /api/devices` create route.
