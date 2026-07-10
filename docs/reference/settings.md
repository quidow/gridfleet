# Settings Reference

This page documents the shipped settings registry. Each setting has a persisted key, category, default, validation metadata, and operational meaning. If an env var is listed, the registry uses that env var as the device default fallback.

## Categories

| Category | Display name | Shipped keys |
| --- | --- | --- |
| `general` | General | 31 |
| `grid` | Appium & Grid | 12 |
| `notifications` | Notifications | 3 |
| `agent` | Agent | 10 |
| `reservations` | Reservations | 4 |
| `retention` | Data Retention | 10 |
| `device_checks` | Device Checks | 3 |

## Registry

| Key | Category | Type | Default | Env var | Validation | Operational meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `general.heartbeat_interval_sec` | `general` | `int` | `15` | `GRIDFLEET_HEARTBEAT_INTERVAL_SEC` | `5..300` | Host-sweep cadence: how often the latest pushed agent status is evaluated, feeding liveness and Appium-node convergence |
| `general.host_offline_after_sec` | `general` | `int` | `45` | none | `15..3600` | Seconds without a status push before a host is marked offline |
| `general.partition_probe_interval_sec` | `general` | `int` | `60` | none | `15..3600` | How often the manager verifies it can reach each online agent (network-partition diagnostic; feeds no liveness state) |
| `general.intent_reconcile_interval_sec` | `general` | `int` | `5` | none | `1..300` | Seconds between intent reconciler full-device scans |
| `general.node_max_failures` | `general` | `int` | `3` | `GRIDFLEET_NODE_MAX_FAILURES` | `1..20` | Consecutive failed node checks before restart or suppression logic runs |
| `general.device_cooldown_max_sec` | `general` | `int` | `3600` | `GRIDFLEET_DEVICE_COOLDOWN_MAX_SEC` | `60..86400` | Maximum run-scoped device cooldown accepted from clients |
| `general.device_cooldown_escalation_threshold` | `general` | `int` | `3` | `GRIDFLEET_DEVICE_COOLDOWN_ESCALATION_THRESHOLD` | `0..100` | Number of cooldowns for the same device within one run before the device is escalated out of the run; the escalated device is placed into maintenance or left available per `general.run_failure_escalates_to_maintenance`; `0` disables escalation |
| `general.run_failure_escalates_to_maintenance` | `general` | `bool` | `true` | none | boolean | When a device is escalated out of a run (CI preparation failure or cooldown threshold exceeded), true places it into maintenance (manual recovery); false leaves it available. The device is released from the run regardless |
| `general.hardware_telemetry_stale_timeout_sec` | `general` | `int` | `900` | none | `60..86400` | How old hardware telemetry can get before the UI marks it stale |
| `general.hardware_temperature_warning_c` | `general` | `int` | `38` | none | `20..100` | Temperature threshold that raises a hardware warning |
| `general.hardware_temperature_critical_c` | `general` | `int` | `42` | none | `20..100` | Temperature threshold that raises a critical hardware alert |
| `general.hardware_telemetry_consecutive_samples` | `general` | `int` | `2` | none | `1..10` | Consecutive warning or critical samples required before escalating hardware health |
| `general.host_resource_telemetry_window_minutes` | `general` | `int` | `60` | none | `5..1440` | Default Host Detail telemetry time window |
| `general.session_viability_interval_sec` | `general` | `int` | `3600` | none | `0..604800` | Interval for idle session-viability probes; `0` disables the loop |
| `general.session_viability_timeout_sec` | `general` | `int` | `120` | none | `10..600` | Timeout for a session-viability probe |
| `general.session_viability_failure_threshold` | `general` | `int` | `3` | none | `1..20` | Consecutive session-viability failures required before the manager parks the device; tolerates transient Appium hiccups |
| `general.fleet_capacity_snapshot_interval_sec` | `general` | `int` | `60` | none | `10..3600` | How often fleet capacity snapshots are recorded |
| `general.background_loop_flush_interval_sec` | `general` | `int` | `15` | none | `1..300` | How often the scheduler flushes in-memory background-loop heartbeat snapshots to the control-plane state table |
| `general.lifecycle_recovery_backoff_base_sec` | `general` | `int` | `60` | none | `1..3600` | Base delay for lifecycle automatic recovery backoff |
| `general.lifecycle_recovery_backoff_max_sec` | `general` | `int` | `900` | none | `1..86400` | Maximum delay for lifecycle automatic recovery backoff |
| `general.lifecycle_recovery_review_threshold` | `general` | `int` | `5` | none | `1..100` | Consecutive automatic recovery failures before the device is shelved into `review_required`; automated recovery loops skip it until an operator action clears the flag |
| `device_checks.ip_ping.consecutive_fail_threshold` | `device_checks` | `int` | `3` | none | `1..50` | Consecutive ICMP-ping misses before an opted-in device is marked unhealthy; set to 1 for strict, no-hysteresis behaviour |
| `device_checks.ip_ping.timeout_sec` | `device_checks` | `float` | `2.0` | none | `0.5..30.0` | Per-attempt ICMP-ping timeout used by the adapter |
| `device_checks.ip_ping.count_per_cycle` | `device_checks` | `int` | `1` | none | `1..10` | Number of ICMP echo requests sent per cycle inside the adapter probe |
| `grid.session_poll_interval_sec` | `grid` | `int` | `30` | none | `1..300` | Interval for the direct-to-Appium session observation sweep that reconciles `Session` rows against live Appium state |
| `grid.queue_timeout_sec` | `grid` | `int` | `300` | `GRIDFLEET_GRID_QUEUE_TIMEOUT_SEC` | `5..3600` | How long a queued new-session request may wait for a device before failing |
| `grid.session_idle_timeout_sec` | `grid` | `int` | `1800` | `GRIDFLEET_GRID_SESSION_IDLE_TIMEOUT_SEC` | `60..86400` | How long a running session may go without reported client activity before the observation sweep terminates it. Replaces the relay's idle timeout (driver enforcement of `newCommandTimeout` is config-dependent), so an abandoned client cannot pin its device busy forever. A client `appium:newCommandTimeout` above this value extends the window per session, up to `grid.session_idle_timeout_ceiling_sec` |
| `grid.session_idle_timeout_ceiling_sec` | `grid` | `int` | `7200` | `GRIDFLEET_GRID_SESSION_IDLE_TIMEOUT_CEILING_SEC` | `60..86400` | Hard ceiling on how far a client's `appium:newCommandTimeout` may extend the idle reap window. `newCommandTimeout=0` ("never idle-kill") clamps here, preserving the zombie-session guarantee. Clients can extend the idle window, never shorten it |
| `grid.session_first_command_grace_sec` | `grid` | `int` | `180` | `GRIDFLEET_GRID_SESSION_FIRST_COMMAND_GRACE_SEC` | `30..3600` | How long a running session whose client never issued a command (NULL `last_activity_at`) may live before the observation sweep terminates it. Measured from the allocation claim (`started_at`), so Appium session-create time eats into the grace. Bounds abandoned-client zombie sessions well below the full idle timeout |
| `grid.claim_window_sec` | `grid` | `int` | `120` | `GRIDFLEET_GRID_CLAIM_WINDOW_SEC` | `5..600` | How long an allocated (pending) session may remain unconfirmed before the `grid_allocation_reaper` fails it. Must exceed worst-case Appium session-creation time |
| `appium.port_range_start` | `grid` | `int` | `4723` | `GRIDFLEET_APPIUM_PORT_RANGE_START` | `1024..65535` | Start of the managed Appium node port range |
| `appium.port_range_end` | `grid` | `int` | `4823` | `GRIDFLEET_APPIUM_PORT_RANGE_END` | `1024..65535` | End of the managed Appium node port range |
| `appium.startup_timeout_sec` | `grid` | `int` | `30` | none | `5..120` | Node startup readiness timeout |
| `appium_reconciler.restart_window_sec` | `grid` | `int` | `120` | none | `30..600` | Wall-clock window within which a fresh `restart_requested_at` watermark projects a node as `restarting` (read-time bounding); past this window a still-unsatisfied watermark self-clears at read time — no sweep |
| `appium_reconciler.host_parallelism` | `grid` | `int` | `8` | none | `1..32` | Maximum number of hosts processed concurrently by the host sweep |
| `appium.session_override` | `grid` | `bool` | `true` | none | boolean | Whether managed Appium nodes force-close lingering sessions before opening a new one |
| `notifications.toast_events` | `notifications` | `json` | `["node.crash","host.heartbeat_lost","device.operational_state_changed","device.hardware_health_changed","run.expired"]` | none | event catalog item list | Event names eligible for frontend toast display |
| `notifications.toast_auto_dismiss_sec` | `notifications` | `int` | `5` | none | `0..60` | Auto-dismiss delay for success toasts; `0` means manual dismissal |
| `notifications.toast_severity_threshold` | `notifications` | `string` | `warning` | none | `info`, `warning`, `error` | Minimum toast severity shown in the UI |
| `agent.min_version` | `agent` | `string` | `0.1.0` | `GRIDFLEET_MIN_AGENT_VERSION` | none | Minimum accepted agent version; empty disables the version check |
| `agent.recommended_version` | `agent` | `string` | `""` | `GRIDFLEET_AGENT_RECOMMENDED_VERSION` | none | Recommended `gridfleet-agent` version surfaced to agents and operators; empty disables recommendation messaging |
| `agent.auto_accept_hosts` | `agent` | `bool` | `true` | `GRIDFLEET_HOST_AUTO_ACCEPT` | boolean | Whether self-registering hosts are automatically approved |
| `agent.default_port` | `agent` | `int` | `5100` | none | `1024..65535` | Default agent port for new hosts |
| `agent.http_pool_enabled` | `agent` | `bool` | `true` | none | boolean | When true, pool one `httpx.AsyncClient` per (host, port) tuple for backend-to-agent calls. When false, every backend→agent call opens a fresh client — dev/debug only; do not disable in production. |
| `agent.http_pool_max_keepalive` | `agent` | `int` | `10` | none | `1..100` | Max keepalive connections per pooled client |
| `agent.http_pool_idle_seconds` | `agent` | `int` | `60` | none | `5..600` | Idle time (seconds) after which a pooled keepalive connection is closed |
| `agent.circuit_breaker_failure_threshold` | `agent` | `int` | `5` | none | `1..50` | Consecutive backend-to-agent failures before the circuit opens |
| `agent.circuit_breaker_cooldown_seconds` | `agent` | `int` | `30` | none | `5..600` | Seconds the circuit stays open before a probe is allowed |
| `reservations.default_ttl_minutes` | `reservations` | `int` | `60` | none | `1..1440` | Default reservation TTL when callers omit it |
| `reservations.max_ttl_minutes` | `reservations` | `int` | `180` | none | `1..1440` | Hard cap for reservation TTL |
| `reservations.default_heartbeat_timeout_sec` | `reservations` | `int` | `120` | none | `30..600` | Default heartbeat timeout for runs |
| `reservations.reaper_interval_sec` | `reservations` | `int` | `15` | `GRIDFLEET_RUN_REAPER_INTERVAL_SEC` | `5..300` | Interval for the stale-run reaper loop |
| `retention.sessions_days` | `retention` | `int` | `14` | none | `1..3650` | Delete completed sessions older than N days |
| `retention.probe_sessions_days` | `retention` | `int` | `7` | none | `1..3650` | Delete probe session rows (diagnostic only) older than N days; separate window from `retention.sessions_days` |
| `retention.audit_log_days` | `retention` | `int` | `180` | none | `1..3650` | Delete device config audit entries older than N days |
| `retention.device_events_days` | `retention` | `int` | `90` | none | `1..3650` | Delete device incident events older than N days |
| `retention.host_resource_telemetry_hours` | `retention` | `int` | `24` | none | `1..720` | Delete host resource telemetry older than N hours |
| `retention.capacity_snapshots_days` | `retention` | `int` | `30` | none | `1..3650` | Delete fleet capacity snapshots older than N days |
| `retention.system_events_days` | `retention` | `int` | `30` | none | `1..3650` | Delete system events older than N days |
| `retention.test_runs_days` | `retention` | `int` | `30` | none | `1..3650` | Delete terminal test runs older than N days; their device reservations cascade |
| `retention.jobs_days` | `retention` | `int` | `30` | none | `1..3650` | Delete completed or failed durable jobs older than N days |
| `retention.cleanup_interval_hours` | `retention` | `int` | `1` | none | `1..168` | Interval for the retention cleanup loop |

## Notes

- Driver registry is an operator tool on the Settings screen, but it is not part of the persisted settings-key registry above.
- `notifications.toast_events` is validated and normalized against the emitted event names documented in [events.md](events.md).
