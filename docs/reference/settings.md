# Settings Reference

This page documents the shipped settings registry. Each setting has a persisted key, category, default, validation metadata, and operational meaning. Registry defaults are code constants; environment variables do not seed registry values.

## Categories

| Category | Display name | Shipped keys |
| --- | --- | --- |
| `general` | General | 15 |
| `grid` | Appium & Allocation | 9 |
| `agent` | Agent | 3 |
| `reservations` | Reservations | 3 |
| `retention` | Data Retention | 9 |
| `device_checks` | Device Checks | 5 |

## Registry

| Key | Category | Type | Default | Validation | Operational meaning |
| --- | --- | --- | --- | --- | --- |
| `general.host_offline_after_sec` | `general` | `int` | `45` | `15..3600` | Seconds without a status push before a host is marked offline |
| `general.node_fail_window_sec` | `general` | `int` | `60` | `0..600` | Wall-clock seconds of failing node-health observations before the node is marked offline and restart escalation runs; `0` flips on the first failure |
| `general.device_cooldown_max_sec` | `general` | `int` | `3600` | `60..86400` | Maximum run-scoped device cooldown accepted from clients |
| `general.device_cooldown_escalation_threshold` | `general` | `int` | `3` | `0..100` | Number of cooldowns for the same device within one run before the device is escalated out of the run; the escalated device is placed into maintenance or left available per `general.run_failure_escalates_to_maintenance`; `0` disables escalation |
| `general.run_failure_escalates_to_maintenance` | `general` | `bool` | `true` | boolean | When a device is escalated out of a run (CI preparation failure or cooldown threshold exceeded), true places it into maintenance (manual recovery); false leaves it available. The device is released from the run regardless |
| `general.session_viability_interval_sec` | `general` | `int` | `3600` | `0..604800` | Interval for idle session-viability probes; `0` disables the loop |
| `general.session_viability_timeout_sec` | `general` | `int` | `120` | `10..600` | Timeout for a session-viability probe |
| `general.session_viability_failure_threshold` | `general` | `int` | `3` | `1..20` | Consecutive session-viability failures required before the manager parks the device; tolerates transient Appium hiccups |
| `general.lifecycle_recovery_backoff_base_sec` | `general` | `int` | `60` | `1..3600` | Base delay for lifecycle automatic recovery backoff |
| `general.lifecycle_recovery_backoff_max_sec` | `general` | `int` | `900` | `1..86400` | Maximum delay for lifecycle automatic recovery backoff |
| `general.lifecycle_recovery_review_threshold` | `general` | `int` | `5` | `1..100` | Consecutive automatic recovery failures before the device is shelved into `review_required`; automated recovery loops skip it until an operator action clears the flag |
| `device_checks.ip_ping.fail_window_sec` | `device_checks` | `int` | `120` | `0..3600` | Wall-clock seconds ICMP ping must keep failing before an opted-in device is marked unhealthy; `0` flips on the first miss |
| `device_checks.probe_unanswered.fail_window_sec` | `device_checks` | `int` | `120` | `0..3600` | Wall-clock seconds unanswered health probes may persist before a device is marked unhealthy; `0` flips on the first miss |
| `device_checks.probe_failed.fail_window_sec` | `device_checks` | `int` | `120` | `0..3600` | Wall-clock seconds a manifest-declared debounceable health check may fail before the device is marked unhealthy; `0` flips on the first miss |
| `device_checks.ip_ping.timeout_sec` | `device_checks` | `float` | `2.0` | `0.5..30.0` | Per-attempt ICMP-ping timeout used by the adapter |
| `device_checks.ip_ping.count_per_cycle` | `device_checks` | `int` | `1` | `1..10` | Number of ICMP echo requests sent per cycle inside the adapter probe |
| `grid.queue_timeout_sec` | `grid` | `int` | `300` | `30..3600` | How long a queued new-session request may wait for a device before failing (must exceed the router's 25s long-poll slice) |
| `grid.session_idle_timeout_sec` | `grid` | `int` | `1800` | `60..86400` | How long a running session may go without reported client activity before the observation sweep terminates it. Replaces the relay's idle timeout (driver enforcement of `newCommandTimeout` is config-dependent), so an abandoned client cannot pin its device busy forever. A client `appium:newCommandTimeout` above this value extends the window per session, up to `grid.session_idle_timeout_ceiling_sec` |
| `grid.session_idle_timeout_ceiling_sec` | `grid` | `int` | `7200` | `60..86400` | Hard ceiling on how far a client's `appium:newCommandTimeout` may extend the idle reap window. `newCommandTimeout=0` ("never idle-kill") clamps here, preserving the zombie-session guarantee. Clients can extend the idle window, never shorten it |
| `grid.session_first_command_grace_sec` | `grid` | `int` | `180` | `30..3600` | How long a running session whose client never issued a command (NULL `last_activity_at`) may live before the observation sweep terminates it. Measured from the allocation claim (`started_at`), so Appium session-create time eats into the grace. Bounds abandoned-client zombie sessions well below the full idle timeout |
| `grid.claim_window_sec` | `grid` | `int` | `120` | `30..600` | How long an allocated (pending) session may remain unconfirmed before it is failed. Must exceed worst-case Appium session-creation time, or in-flight creates get reaped mid-create. The reaper adds a fixed +60s confirm grace on top of this window to absorb router confirm retries. The floor is 30s: the router's create-timeout cap engages only above 10s, so a smaller window lets the orphan sweep race a real in-creation session |
| `appium.port_range_start` | `grid` | `int` | `4723` | `1024..65535` | Start of the port range the backend assigns managed Appium nodes from. Each agent only binds ports inside its own `AGENT_APPIUM_PORT_RANGE_*` env; keep this range within every host's env range |
| `appium.port_range_end` | `grid` | `int` | `4823` | `1024..65535` | End of the port range the backend assigns managed Appium nodes from. Each agent only binds ports inside its own `AGENT_APPIUM_PORT_RANGE_*` env; keep this range within every host's env range |
| `appium.startup_timeout_sec` | `grid` | `int` | `30` | `5..120` | Node startup readiness timeout |
| `appium_reconciler.restart_window_sec` | `grid` | `int` | `120` | `30..600` | Wall-clock window within which a fresh `restart_requested_at` watermark projects a node as `restarting` (read-time bounding); past this window a still-unsatisfied watermark self-clears at read time — no sweep |
| `agent.min_version` | `agent` | `string` | `0.33.0` | none | Minimum accepted agent version; empty disables the version check |
| `agent.recommended_version` | `agent` | `string` | `""` | none | Recommended `gridfleet-agent` version surfaced to agents and operators; empty disables recommendation messaging |
| `agent.auto_accept_hosts` | `agent` | `bool` | `false` | boolean | Whether self-registering hosts are automatically approved; off by default |
| `reservations.default_ttl_minutes` | `reservations` | `int` | `60` | `1..1440` | Default reservation TTL when callers omit it |
| `reservations.max_ttl_minutes` | `reservations` | `int` | `180` | `1..1440` | Hard cap for reservation TTL |
| `reservations.default_heartbeat_timeout_sec` | `reservations` | `int` | `120` | `30..600` | Default heartbeat timeout for runs |
| `retention.sessions_days` | `retention` | `int` | `14` | `1..3650` | Delete completed sessions older than N days |
| `retention.probe_sessions_days` | `retention` | `int` | `7` | `1..3650` | Delete probe session rows (diagnostic only) older than N days; separate window from `retention.sessions_days` |
| `retention.audit_log_days` | `retention` | `int` | `180` | `1..3650` | Delete device config audit entries older than N days |
| `retention.device_events_days` | `retention` | `int` | `90` | `1..3650` | Delete device incident events older than N days |
| `retention.host_resource_telemetry_hours` | `retention` | `int` | `24` | `1..720` | Delete host resource telemetry older than N hours |
| `retention.capacity_snapshots_days` | `retention` | `int` | `30` | `1..3650` | Delete fleet capacity snapshots older than N days |
| `retention.system_events_days` | `retention` | `int` | `30` | `1..3650` | Delete system events older than N days |
| `retention.test_runs_days` | `retention` | `int` | `30` | `1..3650` | Delete terminal test runs older than N days; their device reservations cascade |
| `retention.jobs_days` | `retention` | `int` | `30` | `1..3650` | Delete completed or failed durable jobs older than N days |
## Notes

- Registry defaults are code constants; env vars never seed them (removed 2026-07, WS-4.2).
- Driver registry is an operator tool on the Settings screen, but it is not part of the persisted settings-key registry above.
- Toast behavior is a frontend constant: the event stream shows the built-in five-event default with a warning threshold and five-second dismissal.
