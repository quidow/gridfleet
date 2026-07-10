# Settings And Operational Controls

This guide explains what the Settings page controls, which changes are low-risk versus high-impact, and how the driver catalog fits into daily operations.

## How The Settings Page Works

The Settings page is the control panel for manager-owned device behavior.

Current shipped behaviors:

- each settings tab loads a grouped set of saved keys from `/api/settings`
- edited values are staged locally until you click `Save Changes`
- `Modified` means the saved value differs from the registry default
- the reset icon restores one key to its default
- `Reset All Settings` restores every registry-backed setting to defaults

Use this page for shared fleet behavior. Do not use it as a replacement for device-specific setup or recovery.

## Settings Categories

| Tab | What It Controls | Main Operational Risk |
| --- | --- | --- |
| `General` | manager loops, health timing, session viability, lifecycle backoff | changing these affects fleet-wide recovery and detection timing |
| `Appium & Grid` | Grid URL, Grid polling, Appium port pool, startup timeout | bad values can stop nodes from registering or probing correctly |
| `Notifications` | in-app toast selection and threshold | too many events create alert fatigue; too few hide incidents |
| `Device Defaults` | currently empty — no registry settings are backed under this category yet | none today (no settings render here) |
| `Agent` | host auto-accept, minimum agent version, default port | directly changes host onboarding and version trust behavior |
| `Reservations` | default TTL, heartbeat timeout, stale-run reaper timing | directly affects how long devices stay locked to runs |
| `Data Retention` | cleanup age and cleanup cadence | aggressive values reduce history available for triage |
| `Backup & Restore` | export and import of a device-configuration bundle | importing a stale or wrong bundle can overwrite device config |

## General

The `General` tab controls the manager's background loops and automatic recovery timing.

Most important settings:

- `host_offline_after_sec` (the sweep cadence and partition diagnostic cadence are plumbing constants)
  - together determine how host liveness is derived from agent status pushes (`host_offline_after_sec` since the last push) and how often the reachability probe checks for a network partition
- `node_max_failures`
  - controls how many consecutive node-health failures trigger automatic restart behavior
- `hardware_telemetry_stale_timeout_sec` and `hardware_telemetry_consecutive_samples`
  - control when battery telemetry becomes stale in the UI and how many repeated hot samples are required before escalating a hardware alert
- `host_resource_telemetry_window_minutes`
  - sets the default Host Detail chart window

The observation cadences themselves (node health, device connectivity, property refresh, hardware and host-resource telemetry) are no longer settings: the agent gathers those observations locally on fixed 30/60/300/600 s constants in `agent_app/probes.py` and pushes them in its consolidated status; the manager folds each pushed section into durable facts.
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

- session observation is a fixed plumbing cadence and is not operator-tunable
  - how frequently the direct-to-Appium session observation sweep reconciles `Session` rows
- `grid.queue_timeout_sec`
  - how long a queued new-session request waits for a device before failing
- `grid.claim_window_sec`
  - how long an allocated (pending) session may stay unconfirmed before the allocation reaper fails it
- `appium.port_range_start` and `appium.port_range_end`
  - the host-local Appium port pool used when starting nodes
- `appium.startup_timeout_sec`
  - how long the manager waits for node readiness during start or verification

Use this tab when node start, node registration, or verification probes need fleet-wide adjustment.

## Notifications

The `Notifications` tab controls which recent events generate in-app toast messages and what severity threshold applies.

Practical guidance:

- use toasts for events you want the current operator to notice immediately
- `device.hardware_health_changed` is intended for warning/critical battery conditions; raise its toast priority when labs need to surface overheating or charging anomalies

## Device Defaults

The `Device Defaults` tab is currently empty: no registry settings are backed under the `devices` category, so the panel renders nothing. There are no platform-tag-preset settings here today.

## Agent

The `Agent` tab controls host enrollment and version expectations.

Most important settings:

- `agent.min_version`
  - the minimum version shown as acceptable in Hosts and Host Detail
- `agent.auto_accept_hosts` (off by default; operators approve new hosts manually)
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

This tab directly changes how long devices stay reserved and how forgiving the manager is toward slow or flaky CI heartbeat behavior.

## Data Retention

The `Data Retention` tab controls how long operational history stays available.

Current cleanup buckets (the named `Retention Windows` section):

- completed session history
- audit-log entries
- device-event / lifecycle-incident history
- fleet capacity snapshot history
- host resource telemetry history
- cleanup loop cadence

The registry also defines several additional retention keys (probe sessions, agent reconfigure outbox, agent process logs, device diagnostic snapshots) that surface under the `Other Settings` catch-all rather than the named `Retention Windows` section.

Reduce these values only if storage pressure matters more than long-tail debugging history.

## Drivers

The standalone Drivers page (reached from the main sidebar nav, not from Settings) is the driver-pack catalog.

Operators can:

- see the current installed driver-pack count
- open the Drivers page to upload, inspect, enable, drain, export, or delete driver packs
- use Host Detail for per-host driver runtime status and Run Doctor checks

Host Detail exposes a per-pack `Run Doctor` action (plus any driver-pack-defined feature actions) for per-host driver runtime status.

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
