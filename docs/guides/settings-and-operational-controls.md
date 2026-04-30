# Settings And Operational Controls

This guide explains what the Settings page controls, which changes are low-risk versus high-impact, and how the driver catalog and webhook tools fit into daily operations.

## How The Settings Page Works

The Settings page is the control panel for manager-owned device behavior.

Current shipped behaviors:

- each settings tab loads a grouped set of saved keys from `/api/settings`
- edited values are staged locally until you click `Save Changes`
- `Modified` means the saved value differs from the registry default
- the reset icon restores one key to its default
- `Reset All Settings` restores every registry-backed setting to defaults
- webhook and plugin tabs are operational registries on the same page, not part of the generic settings key registry
- the Drivers tab is a handoff panel that links to the standalone Drivers page

Use this page for shared fleet behavior. Do not use it as a replacement for device-specific setup or recovery.

## Settings Categories

| Tab | What It Controls | Main Operational Risk |
| --- | --- | --- |
| `General` | manager loops, health timing, session viability, lifecycle backoff | changing these affects fleet-wide recovery and detection timing |
| `Appium & Grid` | Grid URL, Grid polling, Appium port pool, startup timeout, default plugins | bad values can stop nodes from registering or probing correctly |
| `Notifications` | in-app toast selection and threshold | too many events create alert fatigue; too few hide incidents |
| `Device Defaults` | default `auto_manage` and platform tag presets for newly added devices | only new devices pick up these defaults automatically |
| `Agent` | host auto-accept, minimum agent version, default port | directly changes host onboarding and version trust behavior |
| `Reservations` | default TTL, heartbeat timeout, stale-run reaper timing | directly affects how long devices stay locked to runs |
| `Data Retention` | cleanup age and cleanup cadence | aggressive values reduce history available for triage |
| `Drivers` | handoff to the standalone driver-pack catalog | stale or disabled packs can leave hosts mismatched for verification or test startup |
| `Webhooks` | outbound event subscriptions and delivery targets | bad endpoints create noisy failures or missed downstream alerts |

## General

The `General` tab controls the manager's background loops and automatic recovery timing.

Most important settings:

- `heartbeat_interval_sec` and `max_missed_heartbeats`
  - together determine how quickly a quiet host becomes `offline`
- `node_check_interval_sec` and `node_max_failures`
  - control how fast node-health failures trigger automatic restart behavior
- `device_check_interval_sec`
  - controls how quickly transport loss is reflected in device state
- `property_refresh_interval_sec`
  - controls how often the manager refreshes dynamic version facts such as OS and software versions
- `hardware_telemetry_interval_sec`, `hardware_telemetry_stale_timeout_sec`, and `hardware_telemetry_consecutive_samples`
  - control how often physical-device battery telemetry is refreshed, when it becomes stale in the UI, and how many repeated hot samples are required before escalating a hardware alert
- `host_resource_telemetry_interval_sec` and `host_resource_telemetry_window_minutes`
  - control how often host CPU/memory/disk telemetry is sampled and the default Host Detail chart window
- `hardware_temperature_warning_c` and `hardware_temperature_critical_c`
  - define the warning and critical overheating thresholds used for `device.hardware_health_changed` events and the Devices UI
- `session_viability_interval_sec` and `session_viability_timeout_sec`
  - control the idle Appium-session probe loop; setting the interval to `0` disables that loop
- `lifecycle_recovery_backoff_base_sec` and `lifecycle_recovery_backoff_max_sec`
  - control how long the manager waits between repeated automatic recovery attempts

Change this tab cautiously. These knobs affect the whole fleet, including dashboard triage noise, automatic recovery cadence, and how quickly hosts or devices look unhealthy.

Hardware telemetry surfaces now use four freshness states:

- `Fresh` means a supported device reported telemetry within the configured stale timeout
- `Stale` means the last supported sample is older than the stale timeout
- `Unsupported` means the current platform/tooling cannot provide battery telemetry for that device
- `Unknown` means the manager has not recorded a successful telemetry sample yet

Host resource telemetry is intentionally lightweight:

- the Host Detail `Diagnostics` tab shows CPU, memory, and disk history only for the recent operational window
- offline hosts or missed polls appear as gaps in the charts, not synthetic zeroes
- there are no built-in alerts in this phase; the goal is operator correlation and triage

## Appium & Grid

The `Appium & Grid` tab controls the shared node process contract.

Most important settings:

- `grid.hub_url`
  - the Grid endpoint the manager uses for node registration, session sync, and probe work
- `grid.session_poll_interval_sec`
  - how frequently Grid session state is polled
- `appium.port_range_start` and `appium.port_range_end`
  - the host-local Appium port pool used when starting nodes
- `appium.default_plugins`
  - comma-separated plugins added to every managed Appium node
- `appium.startup_timeout_sec`
  - how long the manager waits for node readiness during start or verification

Use this tab when node start, node registration, or verification probes need fleet-wide adjustment.

## Notifications And Webhooks

The product has two notification layers:

- `Notifications`
  - controls which recent events generate in-app toast messages and what severity threshold applies
- `Webhooks`
  - controls which events are pushed to external systems

Toast settings are for the operator in the UI right now. Webhooks are for systems such as Slack relays, incident tooling, or custom listeners.

Practical guidance:

- use toasts for events you want the current operator to notice immediately
- use webhooks for persistent downstream handling
- `device.hardware_health_changed` is intended for warning/critical battery conditions; use it when labs need downstream alerting for overheating or charging anomalies
- use `Send test event` before trusting a new webhook target
- disable, do not delete, a webhook when you want a temporary pause without losing its event list

## Device Defaults

The `Device Defaults` tab affects new devices, not the entire existing registry.

Current defaults:

- `devices.default_auto_manage`
  - sets the starting `auto_manage` value for newly created or discovered devices

If you change this value, only later intake work picks up the new default automatically. Existing devices keep their current settings until edited directly or changed through bulk/group actions.

## Agent

The `Agent` tab controls host enrollment and version expectations.

Most important settings:

- `agent.min_version`
  - the minimum version shown as acceptable in Hosts and Host Detail
- `agent.auto_accept_hosts`
  - decides whether self-registering hosts appear immediately as `online` or first as `pending`
- `agent.default_port`
  - the prefilled/default agent port for new host records

This tab matters most during host rollout or when you are tightening operational trust after an agent update.

## Reservations

The `Reservations` tab controls the default behavior of runs.

Most important settings:

- `reservations.default_ttl_minutes`
  - default maximum run duration when the creator does not send a TTL
- `reservations.max_ttl_minutes`
  - hard cap that prevents unusually long reservations
- `reservations.default_heartbeat_timeout_sec`
  - how long a quiet run can go before the reaper expires it
- `reservations.reaper_interval_sec`
  - how often stale runs are checked for timeout or TTL expiry

This tab directly changes how long devices stay reserved and how forgiving the manager is toward slow or flaky CI heartbeat behavior.

## Data Retention

The `Data Retention` tab controls how long operational history stays available.

Current cleanup buckets:

- completed session history
- audit-log entries
- device-event / lifecycle-incident history
- host resource telemetry history
- cleanup loop cadence

Reduce these values only if storage pressure matters more than long-tail debugging history.

## Drivers

The `Drivers` tab in Settings points operators to the standalone Drivers page.

Operators can:

- see the current installed driver-pack count
- open the Drivers page to upload, inspect, enable, drain, export, or delete driver packs
- use Host Detail for per-host driver runtime status and sync actions

Host Detail has a per-host `Sync Drivers` action. Use that when one host is mismatched after the driver-pack catalog changes.

## Safe Change Playbook

When changing settings with device impact:

1. change one category at a time
2. prefer low-traffic periods for Grid, heartbeat, or reservation timing changes
3. watch Dashboard, Hosts, and recent incidents after the save
4. update any CI assumptions if reservation defaults changed

## Related Guides

- [Runs And Reservations](runs-and-reservations.md)
- [Dashboard And Triage](dashboard-and-triage.md)
- [Hosts And Host Detail Operations](hosts-and-host-detail-operations.md)
- [Groups And Bulk Actions](groups-and-bulk-actions.md)
