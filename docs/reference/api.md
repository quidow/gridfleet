# API Reference

This page describes the shipped `/api` surface at a lookup level. It is grouped by domain and focuses on the current contract: method, path, purpose, primary inputs, and primary response shape.

> [!TIP]
> The backend automatically serves an interactive Swagger UI. When running locally, you can explore and test the entire API schema at `http://localhost:8000/docs`. When the login gate is enabled, `/docs`, `/redoc`, `/openapi.json`, and `/metrics` are protected.

Unless noted otherwise, path parameters are UUIDs where the route name implies an entity ID. Validation failures return `422`.

## System

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/health/live` | Liveness probe for the backend process | none | `{"status":"ok"}` |
| `GET` | `/health/ready` | Cluster-aware readiness probe (DB + shared control-plane background-loop heartbeats) | none | object with readiness status and checks |
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
| `GET` | `/api/devices` | List devices with readiness, reservation, lifecycle, and hardware telemetry summary data | filters: `platform_id`, `status`, `reserved`, `host_id`, `identity_value`, `connection_target`, `device_type`, `connection_type`, `os_version`, `search`, `hardware_health_status`, `hardware_telemetry_state`, `needs_attention`, `tags.<key>` | `DeviceRead[]` |
| `GET` | `/api/devices/{device_id}` | Get full device detail | path `device_id` | `DeviceDetail` |
| `PATCH` | `/api/devices/{device_id}` | Apply generic device edits | `DevicePatch` | `DeviceRead` |
| `DELETE` | `/api/devices/{device_id}` | Delete a device | path `device_id` | empty `204` |
| `POST` | `/api/verification/jobs` | Start add-device verification | `DeviceVerificationCreate` | `DeviceVerificationJobRead` (`202`) |
| `POST` | `/api/verification/devices/{device_id}/jobs` | Start re-verification for an existing device | `DeviceVerificationUpdate` | `DeviceVerificationJobRead` (`202`) |
| `GET` | `/api/verification/jobs/{job_id}` | Read verification job state | path `job_id` | `DeviceVerificationJobRead` |
| `GET` | `/api/verification/jobs/{job_id}/events` | Subscribe to verification job SSE updates | path `job_id` | SSE stream |
| `GET` | `/api/devices/{device_id}/capabilities` | Read generated Appium capabilities | path `device_id` | object of Appium capabilities |
| `POST` | `/api/devices/{device_id}/maintenance` | Enter maintenance (stops the node immediately) | `DeviceMaintenanceUpdate` | `DeviceRead` |
| `POST` | `/api/devices/{device_id}/maintenance/exit` | Exit maintenance | path `device_id` | `DeviceRead` |
| `GET` | `/api/devices/{device_id}/config` | Read device config, optionally filtered by key | `keys` | config object |
| `PATCH` | `/api/devices/{device_id}/config` | Deep-merge config keys | partial config object | config object |
| `GET` | `/api/devices/{device_id}/config/history` | Read config audit history | `limit` | config-audit entries |
| `GET` | `/api/devices/{device_id}/test_data` | Read device test data | path `device_id` | `TestDataRead` |
| `PUT` | `/api/devices/{device_id}/test_data` | Replace device test data | test-data object | `TestDataRead` |
| `PATCH` | `/api/devices/{device_id}/test_data` | Deep-merge device test data | partial test-data object | `TestDataRead` |
| `GET` | `/api/devices/{device_id}/test_data/history` | Read test-data audit history | `limit` | `TestDataAuditEntryRead[]` |
| `GET` | `/api/devices/{device_id}/health` | Probe current device health through the assigned host | path `device_id` | health/status object |
| `POST` | `/api/devices/{device_id}/lifecycle/{action}` | Run a pack-defined device lifecycle action | action args object | lifecycle action result |
| `GET` | `/api/devices/{device_id}/logs` | Read device/agent log view | `lines` | log payload |
| `POST` | `/api/devices/{device_id}/node/start` | Start the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/node/stop` | Stop the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/node/restart` | Restart the managed Appium node | path `device_id` | node/device status payload |
| `POST` | `/api/devices/{device_id}/reconnect` | Reconnect a network Android/Fire TV transport | path `device_id` | reconnect result |
| `GET` | `/api/devices/{device_id}/session-outcome-heatmap` | Read recent session outcome points | `days` | `SessionOutcomeHeatmapRow[]` |
| `POST` | `/api/devices/{device_id}/session-test` | Run a session viability probe | path `device_id` | session-test result |

Device list `search` uses PostgreSQL full-text syntax over `name`, identity and connection target fields, manufacturer/model fields, OS version, pack ID, and platform ID. Punctuation in identifiers is tokenized, so `usb-pixel-8-pro` matches searches such as `pixel pro`; quoted phrases, negation, and `OR` follow PostgreSQL `websearch_to_tsquery` behavior. Results still use the requested list ordering rather than relevance ranking.

### Devices — Portability

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/portability/export` | Export the full registered fleet as a versioned JSON bundle | none | portability bundle (`schema_version=1`) |
| `POST` | `/api/portability/import/validate` | Preview an uploaded bundle without writing to the DB | bundle body | per-row classification, host suggestions, `bundle_hash` |
| `POST` | `/api/portability/import` | Commit a previously-validated bundle | `{ bundle, bundle_hash, mappings: [{ index, target_host_id }] }` | `{ created, skipped, failed }` arrays |

`GET /api/portability/export` returns the full registered fleet as a `schema_version=1` JSON bundle. Intended to seed a fresh GridFleet install after a DB wipe or migration. Runtime state is excluded (operational_state, telemetry, lifecycle policy state, verification stamps). The bundle preserves identity, name, tags, device_config, test_data, and the original host hostname.

`POST /api/portability/import/validate` accepts a bundle and returns a preview with no DB writes:

- Per-row classification: `valid_new`, `conflict_skip`, `duplicate_in_bundle`, or `invalid`.
- Auto-matched host suggestions based on case-insensitive hostname comparison against registered hosts.
- A canonical `bundle_hash` to pass back on commit.

`POST /api/portability/import` commits a previously-validated bundle. The server recomputes the canonical bundle hash; a mismatch returns `409`. `mappings` overrides the auto-suggested host assignment per row (identified by `index`). Per-row transaction: device insert and verification job enqueue happen atomically. Response arrays (`created`, `skipped`, `failed`) contain per-index entries with reasons for non-created rows.

### Devices — Inventory

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/portability/inventory` | Streaming read-only export of the live fleet | `format`, `columns`, list filters | CSV or JSON array |

`GET /api/portability/inventory` exports the live fleet including runtime fields (operational_state, telemetry, verification status). Query parameters:

- `format` — `csv` or `json`. CSV serializes JSONB columns as JSON strings; `json` returns a JSON array of nested objects.
- `columns` — comma-separated allowlist of dot-path column names (see `app/portability/schemas.py` for the enum). Omitting or leaving empty returns all columns.
- List filters — `pack_id`, `platform_id`, `status`, `host_id`, `tags.*`, and others mirroring the devices list endpoint.

## Bulk Device Actions

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/devices/bulk/start-nodes` | Start nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/stop-nodes` | Stop nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/restart-nodes` | Restart nodes for many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/update-tags` | Merge or replace tags in bulk | `BulkTagsUpdate` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/delete` | Delete many devices | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/enter-maintenance` | Enter maintenance in bulk | `BulkMaintenanceEnter` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/exit-maintenance` | Exit maintenance in bulk | `BulkDeviceIds` | `BulkOperationResult` |
| `POST` | `/api/devices/bulk/reconnect` | Reconnect eligible network Android devices in bulk | `BulkDeviceIds` | `BulkOperationResult` |

> `effective_state` is a derived field with seven values:
> `starting | running | stopping | stopped | restarting | blocked | error`.
> See [device-lifecycle.md](device-lifecycle.md) for the cascade rules.
> The legacy `state` field was removed in Phase 6.

## Hosts

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/hosts/register` | Host self-registration path used by the agent | `HostRegister` | `HostRead` |
| `POST` | `/api/hosts` | Create a host manually | `HostCreate` | `HostRead` |
| `GET` | `/api/hosts` | List hosts | none | `HostRead[]` |
| `GET` | `/api/hosts/{host_id}` | Read host detail including current devices | path `host_id` | `HostDetail` |
| `GET` | `/api/hosts/{host_id}/diagnostics` | Read backend-owned host diagnostics (breaker state, latest Appium process snapshot, recent agent-local Appium recovery events) | path `host_id` | `HostDiagnosticsRead` |
| `GET` | `/api/hosts/{host_id}/resource-telemetry` | Read recent host CPU, memory, and disk telemetry using backend bucketing | `since`, `until`, `bucket_minutes` | `HostResourceTelemetryResponse` |
| `GET` | `/api/hosts/{host_id}/events` | Read persisted system events scoped to one host | `types`, `since`, `until`, `limit`, `offset` | `HostEventsPage` |
| `GET` | `/api/hosts/{host_id}/tools/status` | Read host agent Node, Node provider, and iOS helper versions | path `host_id` | `HostToolStatusRead` |
| `GET` | `/api/hosts/{host_id}/driver-packs` | Read driver-pack runtime status for one host | path `host_id` | `HostDriverPacksOut` |
| `POST` | `/api/hosts/{host_id}/driver-packs/{pack_id}/doctor` | Trigger driver-pack doctor checks on a host | path `host_id`, `pack_id` | `HostPackDoctorOut[]` |
| `POST` | `/api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}` | Invoke a driver-pack feature action on a host | action args | `FeatureActionResultOut` |
| `GET` | `/api/hosts/{host_id}/tool-env` | Read per-host tool environment variables | path `host_id` | `HostToolEnvRead` |
| `PUT` | `/api/hosts/{host_id}/tool-env` | Set per-host tool environment variables | tool-env object | `HostToolEnvRead` |
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

`GET /api/hosts/{host_id}/events` filters `SystemEvent` rows whose payload contains the requested `host_id`. `types` accepts a comma-separated event-type list.

## Agent Local API

The agent exposes a local `/agent/health` endpoint. The response includes a `version_guidance` object with fields cached from the latest successful manager registration:

- `version_guidance.required_agent_version`: minimum supported agent version from the last successful manager registration.
- `version_guidance.recommended_agent_version`: recommended agent version from the last successful manager registration.
- `version_guidance.agent_version_status`: manager-computed status for the installed agent version (compared against minimum, not recommended).
- `version_guidance.agent_update_available`: `true` when the installed version trails the recommended version.

Each `appium_processes.running_nodes` entry also includes `applied_generation` and `applied_transition_token`. Both are `null` while node pull is disabled. When enabled, they report the last desired generation and transition token applied to that port.

## Agent Appium-Node Desired State

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/agent/appium-nodes/desired` | Agent fetches host-scoped Appium-node intent | `host_id` | `{ nodes: NodeDesiredSpecOut[], generation_hint }` |
| `POST` | `/agent/appium-nodes/refresh` | Wake the agent node poller | none | `{ accepted: true }` (`202`) |

Running specs include the complete `launch` payload. Stopped specs use `launch: null`. A node whose launch inputs cannot be resolved also uses `launch: null` and includes `unrunnable_reason`; one blocked node does not fail the host response. The refresh endpoint is a hint and returns `202` even when the pull loop is disabled.

Agents advertise `node_desired_pull: 1` only when `AGENT_NODE_PULL_ENABLED=true`. Enabled by default — pull is the only node-orchestration mode; there is no backend push path.

## Agent Driver-Pack State

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/agent/driver-packs/desired` | Agent fetches desired driver-pack runtime state | `host_id` | desired pack/runtime payload |
| `POST` | `/agent/driver-packs/status` | Agent reports installed runtimes, pack status, doctor checks, and sidecars | status payload | empty `204` |

## Driver Packs

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/driver-packs/catalog` | List the driver-pack catalog | none | `PackCatalog` |
| `GET` | `/api/driver-packs/{pack_id}` | Read one driver pack | path `pack_id` | `PackOut` |
| `GET` | `/api/driver-packs/{pack_id}/hosts` | List hosts that have this pack installed | path `pack_id` | `DriverPackHostsOut` |
| `PATCH` | `/api/driver-packs/{pack_id}` | Transition pack lifecycle state | `PackPatch`, `override` | `PackOut` |
| `PATCH` | `/api/driver-packs/{pack_id}/policy` | Update pack runtime policy | `RuntimePolicyPatch` | `PackOut` |
| `DELETE` | `/api/driver-packs/{pack_id}` | Delete a driver pack | path `pack_id` | empty `204` |
| `POST` | `/api/driver-packs/uploads` | Upload a driver-pack tarball | tarball upload | `PackOut` (`201`) |
| `GET` | `/api/driver-packs/{pack_id}/releases` | List releases for a pack | path `pack_id` | `PackReleasesOut` |
| `PATCH` | `/api/driver-packs/{pack_id}/releases/current` | Set the current release for a pack | release selector | `PackOut` |
| `DELETE` | `/api/driver-packs/{pack_id}/releases/{release}` | Delete one release | path `pack_id`, `release` | empty `204` |
| `GET` | `/api/driver-packs/{pack_id}/releases/{release}/tarball` | Download a release tarball | path `pack_id`, `release` | tarball stream |
| `POST` | `/api/driver-packs/{pack_id}/releases/{release}/export` | Export a release as a gzip tarball | path `pack_id`, `release` | tarball stream (`200`) |

## Runs

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/runs` | Create a reservation run; add `?include=config` to inline config per device | `RunCreate` | `RunCreateResponse` (`201`) |
| `GET` | `/api/runs` | List runs | filters: `state`, `created_from`, `created_to`, `limit`, `offset`, `sort_by`, `sort_dir` | `{ items: RunRead[], total, limit, offset }` |
| `GET` | `/api/runs/{run_id}` | Read full run detail | path `run_id` | `RunDetail` |
| `POST` | `/api/runs/{run_id}/ready` | Compatibility alias that transitions a preparing run to `active` | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/active` | Transition run from `preparing` to `active`, marking that real testing has begun. Not a gate on device access: run-scoped sessions (`/run/{run_id}/session`) can run on the run's reserved devices — and are linked to the run — from `preparing` onward. | path `run_id` | `RunRead` |
| `POST` | `/api/runs/{run_id}/devices/{device_id}/preparation-failed` | Exclude one reserved device after CI preparation failure, persist the exact failure message, and mark the device unhealthy/offline | `RunPreparationFailureReport` | `RunRead` |
| `POST` | `/api/runs/{run_id}/devices/{device_id}/cooldown` | Put one reserved device into cooldown for the run | `RunCooldownRequest` | cooldown result (`200`) |
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

## Sessions

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/sessions` | List recorded Appium sessions | filters: `device_id`, `status`, `pack_id`, `platform_id`, `run_id`, `started_after`, `started_before`, `limit`, `offset`, `cursor`, `direction`, `sort_by`, `sort_dir`, `include_probes` | `{ items: SessionDetail[], total, limit, offset }` |
| `GET` | `/api/sessions/{session_id}` | Read one recorded session | path `session_id` | `SessionDetail` |
| `PATCH` | `/api/sessions/{session_id}/status` | Write final session status from an external test harness | `SessionStatusUpdate` | `SessionRead` |

Session rows are created by the router/grid allocation flow (via the internal grid API), not by clients. `SessionRead` / `SessionDetail` expose setup-attempt fields populated from the requested capabilities alongside the existing session metadata:

- `status`
- `requested_pack_id`
- `requested_platform_id`
- `requested_device_type`
- `requested_connection_type`
- `requested_capabilities`
- `error_type`
- `error_message`

`SessionDetail` also exposes:

- `is_probe` (bool, default `false`) — true when the row represents a diagnostic probe session (session viability, node health, or device verification). Identified by `test_name == "__gridfleet_probe__"`.
- `probe_checked_by` (string, optional) — probe source: `scheduled`, `manual`, `recovery`, `node_health`, or `verification`. Sourced from `requested_capabilities["gridfleet:probeCheckedBy"]`.

`GET /api/sessions` accepts `include_probes` (bool, default `false`). When omitted, probe sessions are hidden. Setting `include_probes=true` returns probes alongside real sessions. Probe rows never count toward success-rate, throughput, utilization, error breakdown, or heatmap analytics regardless of this flag. Per-device session listing uses the `device_id` query filter on `GET /api/sessions`.

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

## Events

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/events/catalog` | Read the canonical emitted-event catalog | none | event catalog object |
| `GET` | `/api/events` | Subscribe to live SSE events | optional `types`, `device_ids` | SSE stream |
| `GET` | `/api/notifications` | Read recent notification history in newest-first order | `limit`, `offset`, optional `types`, optional `severity` | `{ items: SystemEventRead[], total, limit, offset }` |

`GET /api/notifications` returns a paginated list of `SystemEventRead` objects. Each object includes a top-level `severity` field alongside `id`, `type`, `timestamp`, and `data`:

```json
{
  "items": [
    {
      "id": "0d5f0af1-7c2b-4ec4-98c3-90cf7b0d52ef",
      "type": "device.operational_state_changed",
      "timestamp": "2026-05-16T01:30:00Z",
      "severity": "info",
      "data": {
        "device_id": "uuid",
        "device_name": "Lab Fire TV",
        "old_operational_state": "offline",
        "new_operational_state": "available"
      }
    }
  ],
  "total": 1,
  "limit": 25,
  "offset": 0
}
```

Rows persisted before the severity field was introduced will have `severity = null`. Clients that require a non-null value should fall back to the `default_severity` for that event type from `GET /api/events/catalog`.

### Query parameters

- `limit` — page size; integer between 1 and 200; defaults to 25.
- `offset` — page offset; non-negative integer; defaults to 0.
- `types` — comma-separated event-type names. Only events whose `type` matches one of the supplied names are returned.
- `severity` — comma-separated severity values. Allowed: `info`, `success`, `warning`, `critical`, `neutral`. Rows with `severity = null` (pre-severity-rollout legacy events) are **excluded** when this filter is active. Empty or missing returns all severities. Unknown values produce a `400` response.

Filters compose with AND semantics. Example: `GET /api/notifications?types=node.crash&severity=critical,warning` returns `node.crash` rows whose severity is either `critical` or `warning`.

`GET /api/events/catalog` returns an object whose keys are event type names. Each entry now includes:

- `default_severity` — the severity the backend assigns to this event type when no context-specific override applies. One of `info`, `success`, `warning`, `critical`, `neutral`.
- `allowed_severities` — the set of severity values this event type may carry. The backend will never emit a severity outside this set for this event type. Clients can use this to validate or filter incoming events.

`DeviceRead` / `DeviceDetail` expose device state as `operational_state` (one of `available`, `busy`, `offline`, `verifying`, `maintenance`). Reservation is exposed separately as the boolean `is_reserved` plus an optional `reservation` object.

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

- `pack_id`
- `platform_id`
- `status`
- `host_id`
- `identity_value`
- `connection_target`
- `device_type`
- `connection_type`
- `os_version`
- `os_version_display`
- `hardware_health_status`
- `hardware_telemetry_state`
- `needs_attention`
- `tags` as a key/value object

## Grid, Analytics, And Lifecycle

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/grid/status` | Read session/registry/device summary (served from Postgres, not a Grid hub) | none | grid/registry summary object |
| `GET` | `/api/grid/queue` | Read queued new-session requests (from Postgres allocation state) | none | queue summary object |
| `GET` | `/api/analytics/sessions/summary` | Read aggregated session analytics | `date_from`, `date_to`, `group_by`, `format` | `SessionSummaryRow[]` |
| `GET` | `/api/analytics/devices/utilization` | Read device utilization analytics | `date_from`, `date_to`, `format` | `DeviceUtilizationRow[]` |
| `GET` | `/api/analytics/devices/reliability` | Read device reliability analytics | `date_from`, `date_to`, `format` | `DeviceReliabilityRow[]` |
| `GET` | `/api/analytics/fleet/overview` | Read aggregate fleet analytics | `date_from`, `date_to` | `FleetOverview` |
| `GET` | `/api/analytics/fleet/capacity-timeline` | Read fleet capacity time series | `date_from`, `date_to`, `bucket_minutes` | `FleetCapacityTimeline` |
| `GET` | `/api/lifecycle/incidents` | Read recent lifecycle incident history | `limit`, optional `device_id`, `cursor`, `direction` | `LifecycleIncidentListRead` (`{ items: LifecycleIncidentRead[], limit, next_cursor, prev_cursor }`) |

## Admin

| Method | Path | Purpose | Main input | Primary response |
| --- | --- | --- | --- | --- |
| `POST` | `/api/admin/appium-nodes/{node_id}/clear-transition` | Clear a stuck Appium node desired-state transition | path `node_id` | `AppiumNodeRead` |

## Notes

- The API contract is currently owned by the backend code and FastAPI schemas, not by a separate versioned OpenAPI publishing pipeline.
- Backend responses include `X-Request-ID` so operators can correlate logs and backend-to-agent calls.
- See [events.md](events.md) for the `/api/events` envelope and the emitted event names.
- The current product keeps add-device and re-verification flows behind verification-job routes instead of exposing a general-purpose `POST /api/devices` create route.
