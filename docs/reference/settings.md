# Settings Reference

This page documents the shipped settingss registry. Each setting has a persisted key, category, default, validation metadata, and operational meaning. If an env var is listed, the registry uses that env var as the device default fallback.

## Categories

| Category | Display name | Shipped keys |
| --- | --- | --- |
| `general` | General | 13 |
| `grid` | Appium & Grid | 11 |
| `notifications` | Notifications | 3 |
| `devices` | Device Defaults | 2 |
| `agent` | Agent | 4 |
| `reservations` | Reservations | 5 |
| `retention` | Data Retention | 4 |

## Registry

| Key | Category | Type | Default | Env var | Validation | Operational meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `general.heartbeat_interval_sec` | `general` | `int` | `15` | `GRIDFLEET_HEARTBEAT_INTERVAL_SEC` | `5..300` | How often the manager pings agents |
| `general.max_missed_heartbeats` | `general` | `int` | `3` | `GRIDFLEET_MAX_MISSED_HEARTBEATS` | `1..20` | Missed agent pings before a host is marked offline |
| `general.node_check_interval_sec` | `general` | `int` | `30` | none | `10..600` | Interval for managed Appium node health checks |
| `general.node_max_failures` | `general` | `int` | `3` | none | `1..20` | Consecutive failed node checks before restart or suppression logic runs |
| `general.device_check_interval_sec` | `general` | `int` | `60` | none | `10..600` | Interval for host-reported device connectivity checks |
| `general.session_queue_timeout_sec` | `general` | `int` | `300` | `GRIDFLEET_SESSION_QUEUE_TIMEOUT_SEC` | `30..3600` | Timeout budget for Grid session queueing |
| `general.device_cooldown_max_sec` | `general` | `int` | `3600` | `GRIDFLEET_DEVICE_COOLDOWN_MAX_SEC` | `60..86400` | Maximum run-scoped device cooldown accepted from clients |
| `general.claim_default_retry_after_sec` | `general` | `int` | `5` | `GRIDFLEET_CLAIM_DEFAULT_RETRY_AFTER_SEC` | `1..300` | Retry-After value returned when no run devices are claimable |
| `general.property_refresh_interval_sec` | `general` | `int` | `600` | `GRIDFLEET_PROPERTY_REFRESH_INTERVAL_SEC` | `60..7200` | Interval for background property refresh |
| `general.session_viability_interval_sec` | `general` | `int` | `86400` | none | `0..604800` | Interval for idle session-viability probes; `0` disables the loop |
| `general.session_viability_timeout_sec` | `general` | `int` | `120` | none | `10..600` | Timeout for a session-viability probe |
| `general.lifecycle_recovery_backoff_base_sec` | `general` | `int` | `60` | none | `1..3600` | Base delay for lifecycle automatic recovery backoff |
| `general.lifecycle_recovery_backoff_max_sec` | `general` | `int` | `900` | none | `1..86400` | Maximum delay for lifecycle automatic recovery backoff |
| `grid.hub_url` | `grid` | `string` | `http://selenium-hub:4444` | `GRIDFLEET_GRID_HUB_URL` | none | Selenium Grid hub URL used by the manager and managed nodes |
| `grid.session_poll_interval_sec` | `grid` | `int` | `5` | none | `1..60` | Poll interval for Grid session sync |
| `grid.selenium_jar_version` | `grid` | `string` | `4.41.0` | none | none | Target Selenium Server JAR version for host agents; empty disables management |
| `appium.port_range_start` | `grid` | `int` | `4723` | `GRIDFLEET_APPIUM_PORT_RANGE_START` | `1024..65535` | Start of the managed Appium node port range |
| `appium.port_range_end` | `grid` | `int` | `4823` | `GRIDFLEET_APPIUM_PORT_RANGE_END` | `1024..65535` | End of the managed Appium node port range |
| `appium.default_plugins` | `grid` | `string` | empty string | none | none | Comma-separated Appium plugins added to every managed node |
| `appium.target_version` | `grid` | `string` | `3.3.0` | none | none | Target Appium binary version installed by host agents; empty disables management |
| `appium.startup_timeout_sec` | `grid` | `int` | `30` | none | `5..120` | Node startup readiness timeout |
| `appium.reservation_ttl_sec` | `grid` | `int` | `900` | none | `180..7200` | TTL for temporary Appium parallel-resource reservations; must exceed `appium.startup_timeout_sec + 5s` |
| `appium.resource_sweeper_interval_sec` | `grid` | `int` | `300` | none | `30..3600` | Cadence of the leader-only loop that reaps expired temporary Appium resource claims |
| `appium.session_override` | `grid` | `bool` | `true` | none | boolean | Whether managed Appium nodes force-close lingering sessions before opening a new one |
| `notifications.toast_events` | `notifications` | `json` | `["node.crash","host.heartbeat_lost","device.availability_changed","run.expired"]` | none | event catalog item list | Event names eligible for frontend toast display |
| `notifications.toast_auto_dismiss_sec` | `notifications` | `int` | `5` | none | `0..60` | Auto-dismiss delay for success toasts; `0` means manual dismissal |
| `notifications.toast_severity_threshold` | `notifications` | `string` | `warning` | none | `info`, `warning`, `error` | Minimum toast severity shown in the UI |
| `devices.default_auto_manage` | `devices` | `bool` | `true` | none | boolean | Default `auto_manage` value for newly imported or verified devices |
| `agent.min_version` | `agent` | `string` | `0.1.0` | `GRIDFLEET_MIN_AGENT_VERSION` | none | Minimum accepted agent version; empty disables the version check |
| `agent.recommended_version` | `agent` | `string` | `""` | `GRIDFLEET_AGENT_RECOMMENDED_VERSION` | none | Recommended `gridfleet-agent` version surfaced to agents and operators; empty disables recommendation messaging |
| `agent.auto_accept_hosts` | `agent` | `bool` | `true` | `GRIDFLEET_HOST_AUTO_ACCEPT` | boolean | Whether self-registering hosts are automatically approved |
| `agent.default_port` | `agent` | `int` | `5100` | none | `1024..65535` | Default agent port for new hosts |
| `reservations.default_ttl_minutes` | `reservations` | `int` | `60` | none | `1..1440` | Default reservation TTL when callers omit it |
| `reservations.max_ttl_minutes` | `reservations` | `int` | `180` | none | `1..1440` | Hard cap for reservation TTL |
| `reservations.default_heartbeat_timeout_sec` | `reservations` | `int` | `120` | none | `30..600` | Default heartbeat timeout for runs |
| `reservations.claim_ttl_seconds` | `reservations` | `int` | `120` | `GRIDFLEET_RESERVATION_CLAIM_TTL_SECONDS` | `10..3600` | Claim lease duration before a stale worker claim can be reclaimed |
| `reservations.reaper_interval_sec` | `reservations` | `int` | `15` | `GRIDFLEET_RUN_REAPER_INTERVAL_SEC` | `5..300` | Interval for the stale-run reaper loop |
| `retention.sessions_days` | `retention` | `int` | `90` | none | `1..3650` | Delete completed sessions older than N days |
| `retention.audit_log_days` | `retention` | `int` | `180` | none | `1..3650` | Delete device config audit entries older than N days |
| `retention.device_events_days` | `retention` | `int` | `90` | none | `1..3650` | Delete device incident events older than N days |
| `retention.cleanup_interval_hours` | `retention` | `int` | `24` | none | `1..168` | Interval for the retention cleanup loop |

## Notes

- Driver registry and webhook registry are operator tools on the Settings screen, but they are not part of the persisted settings-key registry above.
- `notifications.toast_events` is validated and normalized against the emitted event names documented in [events-and-webhooks.md](events-and-webhooks.md).
