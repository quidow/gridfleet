"""Registry of all known settings with metadata, types, defaults, and validation."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.services.event_catalog import DEFAULT_TOAST_EVENT_NAMES, PUBLIC_EVENT_NAMES

if TYPE_CHECKING:
    from app.type_defs import SettingValue


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    category: str
    setting_type: str  # "int", "string", "bool", "json"
    default: SettingValue
    description: str
    env_var: str | None = None  # maps to GRIDFLEET_ env var for fallback
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: list[str] | None = field(default=None)
    item_allowed_values: list[str] | None = field(default=None)
    json_list_item_type: str | None = None
    reject_item_prefixes: list[str] | None = field(default=None)


# Ordered display names for UI tabs
CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "general": "General",
    "grid": "Appium & Grid",
    "notifications": "Notifications",
    "devices": "Device Defaults",
    "agent": "Agent",
    "reservations": "Reservations",
    "retention": "Data Retention",
    "device_checks": "Device Checks",
}

_DEFINITIONS: list[SettingDefinition] = [
    # ── General ──
    SettingDefinition(
        key="general.heartbeat_interval_sec",
        category="general",
        setting_type="int",
        default=15,
        description="How often the manager pings agents",
        env_var="GRIDFLEET_HEARTBEAT_INTERVAL_SEC",
        min_value=5,
        max_value=300,
    ),
    SettingDefinition(
        key="general.leader_keepalive_enabled",
        category="general",
        setting_type="bool",
        default=True,
        description=(
            "When true, the elected leader writes a heartbeat row every keepalive_interval_sec "
            "and non-leaders preempt on staleness. Disable to fall back to "
            "kernel-TCP-keepalive-driven failover."
        ),
    ),
    SettingDefinition(
        key="general.leader_keepalive_interval_sec",
        category="general",
        setting_type="int",
        default=5,
        description="How often (seconds) the elected leader writes a heartbeat row",
        min_value=1,
        max_value=60,
    ),
    SettingDefinition(
        key="general.leader_stale_threshold_sec",
        category="general",
        setting_type="int",
        default=30,
        description="Heartbeat older than this many seconds allows non-leader preemption",
        min_value=10,
        max_value=600,
    ),
    SettingDefinition(
        key="general.max_missed_heartbeats",
        category="general",
        setting_type="int",
        default=3,
        description="Missed pings before marking host offline",
        env_var="GRIDFLEET_MAX_MISSED_HEARTBEATS",
        min_value=1,
        max_value=20,
    ),
    SettingDefinition(
        key="general.node_check_interval_sec",
        category="general",
        setting_type="int",
        default=30,
        description="How often Appium node health is checked",
        env_var="GRIDFLEET_NODE_CHECK_INTERVAL_SEC",
        min_value=10,
        max_value=600,
    ),
    SettingDefinition(
        key="general.node_max_failures",
        category="general",
        setting_type="int",
        default=3,
        description="Failed health checks before auto-restart",
        env_var="GRIDFLEET_NODE_MAX_FAILURES",
        min_value=1,
        max_value=20,
    ),
    SettingDefinition(
        key="general.device_check_interval_sec",
        category="general",
        setting_type="int",
        default=60,
        description="How often device connectivity is verified",
        env_var="GRIDFLEET_DEVICE_CHECK_INTERVAL_SEC",
        min_value=10,
        max_value=600,
    ),
    SettingDefinition(
        key="general.session_queue_timeout_sec",
        category="general",
        setting_type="int",
        default=300,
        description="Grid session queue timeout",
        env_var="GRIDFLEET_SESSION_QUEUE_TIMEOUT_SEC",
        min_value=30,
        max_value=3600,
    ),
    SettingDefinition(
        key="general.device_cooldown_max_sec",
        category="general",
        setting_type="int",
        default=3600,
        description="Maximum run-scoped device cooldown accepted from clients",
        env_var="GRIDFLEET_DEVICE_COOLDOWN_MAX_SEC",
        min_value=60,
        max_value=86400,
    ),
    SettingDefinition(
        key="general.device_cooldown_escalation_threshold",
        category="general",
        setting_type="int",
        default=3,
        description=(
            "Number of cooldowns of the same device within one run before the device is "
            "escalated to maintenance and excluded from the run. Set to 0 to disable."
        ),
        env_var="GRIDFLEET_DEVICE_COOLDOWN_ESCALATION_THRESHOLD",
        min_value=0,
        max_value=100,
    ),
    SettingDefinition(
        key="general.claim_default_retry_after_sec",
        category="general",
        setting_type="int",
        default=5,
        description="Retry-After value returned when no run devices are claimable",
        env_var="GRIDFLEET_CLAIM_DEFAULT_RETRY_AFTER_SEC",
        min_value=1,
        max_value=300,
    ),
    SettingDefinition(
        key="general.property_refresh_interval_sec",
        category="general",
        setting_type="int",
        default=600,
        description="How often device properties are refreshed",
        env_var="GRIDFLEET_PROPERTY_REFRESH_INTERVAL_SEC",
        min_value=60,
        max_value=7200,
    ),
    SettingDefinition(
        key="general.hardware_telemetry_interval_sec",
        category="general",
        setting_type="int",
        default=300,
        description="How often hardware telemetry is refreshed",
        min_value=30,
        max_value=7200,
    ),
    SettingDefinition(
        key="general.hardware_telemetry_stale_timeout_sec",
        category="general",
        setting_type="int",
        default=900,
        description="How old hardware telemetry can get before the UI marks it stale",
        min_value=60,
        max_value=86400,
    ),
    SettingDefinition(
        key="general.hardware_temperature_warning_c",
        category="general",
        setting_type="int",
        default=38,
        description="Temperature threshold that raises a hardware warning",
        min_value=20,
        max_value=100,
    ),
    SettingDefinition(
        key="general.hardware_temperature_critical_c",
        category="general",
        setting_type="int",
        default=42,
        description="Temperature threshold that raises a critical hardware alert",
        min_value=20,
        max_value=100,
    ),
    SettingDefinition(
        key="general.hardware_telemetry_consecutive_samples",
        category="general",
        setting_type="int",
        default=2,
        description="Consecutive warning or critical samples required before escalating hardware health",
        min_value=1,
        max_value=10,
    ),
    SettingDefinition(
        key="general.host_resource_telemetry_interval_sec",
        category="general",
        setting_type="int",
        default=60,
        description="How often host resource telemetry is refreshed",
        min_value=15,
        max_value=3600,
    ),
    SettingDefinition(
        key="general.host_resource_telemetry_window_minutes",
        category="general",
        setting_type="int",
        default=60,
        description="Default Host Detail telemetry time window",
        min_value=5,
        max_value=1440,
    ),
    SettingDefinition(
        key="general.session_viability_interval_sec",
        category="general",
        setting_type="int",
        default=3600,
        description="How often idle devices are probed with a real Appium session (0 disables)",
        min_value=0,
        max_value=604800,
    ),
    SettingDefinition(
        key="general.session_viability_timeout_sec",
        category="general",
        setting_type="int",
        default=120,
        description="How long to wait for an Appium session probe before failing",
        min_value=10,
        max_value=600,
    ),
    SettingDefinition(
        key="general.fleet_capacity_snapshot_interval_sec",
        category="general",
        setting_type="int",
        default=60,
        description="How often fleet capacity snapshots are recorded",
        min_value=10,
        max_value=3600,
    ),
    SettingDefinition(
        key="general.lifecycle_recovery_backoff_base_sec",
        category="general",
        setting_type="int",
        default=60,
        description="Base delay for automatic lifecycle recovery backoff",
        min_value=1,
        max_value=3600,
    ),
    SettingDefinition(
        key="general.lifecycle_recovery_backoff_max_sec",
        category="general",
        setting_type="int",
        default=900,
        description="Maximum delay for automatic lifecycle recovery backoff",
        min_value=1,
        max_value=86400,
    ),
    # ── Device Checks ──
    SettingDefinition(
        key="device_checks.ip_ping.consecutive_fail_threshold",
        category="device_checks",
        setting_type="int",
        default=3,
        description=(
            "Consecutive ICMP-ping misses before an opted-in device is marked unhealthy. "
            "Set to 1 for strict, no-hysteresis behaviour."
        ),
        min_value=1,
        max_value=50,
    ),
    SettingDefinition(
        key="device_checks.ip_ping.timeout_sec",
        category="device_checks",
        setting_type="float",
        default=2.0,
        description="Per-attempt ICMP-ping timeout used by the adapter.",
        min_value=0.5,
        max_value=30.0,
    ),
    SettingDefinition(
        key="device_checks.ip_ping.count_per_cycle",
        category="device_checks",
        setting_type="int",
        default=1,
        description="Number of ICMP echo requests sent per cycle inside the adapter probe.",
        min_value=1,
        max_value=10,
    ),
    # ── Appium & Grid ──
    SettingDefinition(
        key="grid.hub_url",
        category="grid",
        setting_type="string",
        default="http://selenium-hub:4444",
        description="Selenium Grid hub URL",
        env_var="GRIDFLEET_GRID_HUB_URL",
    ),
    SettingDefinition(
        key="grid.session_poll_interval_sec",
        category="grid",
        setting_type="int",
        default=5,
        description="How often the manager polls Grid for sessions",
        min_value=1,
        max_value=60,
    ),
    SettingDefinition(
        key="grid.selenium_jar_version",
        category="grid",
        setting_type="string",
        default="4.41.0",
        description="Target Selenium Server JAR version for host agents (empty = unmanaged)",
    ),
    SettingDefinition(
        key="appium.port_range_start",
        category="grid",
        setting_type="int",
        default=4723,
        description="Start of Appium port range",
        env_var="GRIDFLEET_APPIUM_PORT_RANGE_START",
        min_value=1024,
        max_value=65535,
    ),
    SettingDefinition(
        key="appium.port_range_end",
        category="grid",
        setting_type="int",
        default=4823,
        description="End of Appium port range",
        env_var="GRIDFLEET_APPIUM_PORT_RANGE_END",
        min_value=1024,
        max_value=65535,
    ),
    SettingDefinition(
        key="appium.default_plugins",
        category="grid",
        setting_type="string",
        default="",
        description="Comma-separated Appium plugins for all nodes",
    ),
    SettingDefinition(
        key="appium.target_version",
        category="grid",
        setting_type="string",
        default="3.3.0",
        description="Target Appium binary version installed by host agents (empty = unmanaged)",
    ),
    SettingDefinition(
        key="appium.startup_timeout_sec",
        category="grid",
        setting_type="int",
        default=30,
        description="How long to wait for Appium node readiness",
        min_value=5,
        max_value=120,
    ),
    SettingDefinition(
        key="appium.reservation_ttl_sec",
        category="grid",
        setting_type="int",
        default=900,
        description=(
            "TTL for temporary Appium parallel-resource reservations. Must exceed appium.startup_timeout_sec + 5s."
        ),
        min_value=180,
        max_value=7200,
    ),
    SettingDefinition(
        key="appium.resource_sweeper_interval_sec",
        category="grid",
        setting_type="int",
        default=300,
        description="How often expired Appium resource claims are reaped",
        min_value=30,
        max_value=3600,
    ),
    SettingDefinition(
        key="appium.session_override",
        category="grid",
        setting_type="bool",
        default=True,
        description="Whether managed Appium nodes should force-close lingering sessions before opening a new one",
    ),
    # ── Notifications ──
    SettingDefinition(
        key="notifications.toast_events",
        category="notifications",
        setting_type="json",
        default=list(DEFAULT_TOAST_EVENT_NAMES),
        description="Which event types trigger toast notifications",
        item_allowed_values=list(PUBLIC_EVENT_NAMES),
    ),
    SettingDefinition(
        key="notifications.toast_auto_dismiss_sec",
        category="notifications",
        setting_type="int",
        default=5,
        description="Auto-dismiss delay for success toasts (0 = manual only)",
        min_value=0,
        max_value=60,
    ),
    SettingDefinition(
        key="notifications.toast_severity_threshold",
        category="notifications",
        setting_type="string",
        default="warning",
        description="Minimum severity for toasts: info, warning, error",
        allowed_values=["info", "warning", "error"],
    ),
    # ── Device Defaults ──
    SettingDefinition(
        key="devices.default_auto_manage",
        category="devices",
        setting_type="bool",
        default=True,
        description="Default auto_manage value for newly discovered devices",
    ),
    # ── Agent ──
    SettingDefinition(
        key="agent.min_version",
        category="agent",
        setting_type="string",
        default="0.1.0",
        description="Minimum required agent version (empty = no check)",
        env_var="GRIDFLEET_MIN_AGENT_VERSION",
    ),
    SettingDefinition(
        key="agent.recommended_version",
        category="agent",
        setting_type="string",
        default="",
        description="Recommended agent version shown to operators and agents (empty = no recommendation)",
        env_var="GRIDFLEET_AGENT_RECOMMENDED_VERSION",
    ),
    SettingDefinition(
        key="agent.auto_accept_hosts",
        category="agent",
        setting_type="bool",
        default=True,
        description="Auto-accept self-registering hosts",
        env_var="GRIDFLEET_HOST_AUTO_ACCEPT",
    ),
    SettingDefinition(
        key="agent.default_port",
        category="agent",
        setting_type="int",
        default=5100,
        description="Default agent port for new hosts",
        min_value=1024,
        max_value=65535,
    ),
    SettingDefinition(
        key="agent.enable_web_terminal",
        category="agent",
        setting_type="bool",
        default=False,
        description="Enable the host web terminal (backend WebSocket proxy to agent PTY)",
        env_var="GRIDFLEET_ENABLE_WEB_TERMINAL",
    ),
    SettingDefinition(
        key="agent.web_terminal_allowed_origins",
        category="agent",
        setting_type="string",
        default="",
        description=(
            "Comma-separated allowed browser origins for the terminal WebSocket "
            "(empty = block all when auth is enabled)"
        ),
        env_var="GRIDFLEET_WEB_TERMINAL_ALLOWED_ORIGINS",
    ),
    SettingDefinition(
        key="agent.http_pool_enabled",
        category="agent",
        setting_type="bool",
        default=True,
        description="When true, pool one httpx.AsyncClient per (host, port) tuple for backend->agent calls",
    ),
    SettingDefinition(
        key="agent.http_pool_max_keepalive",
        category="agent",
        setting_type="int",
        default=10,
        description="Max keepalive connections per pooled client",
        min_value=1,
        max_value=100,
    ),
    SettingDefinition(
        key="agent.http_pool_idle_seconds",
        category="agent",
        setting_type="int",
        default=60,
        description="Idle time (seconds) after which a pooled keepalive connection is closed",
        min_value=5,
        max_value=600,
    ),
    SettingDefinition(
        key="agent.circuit_breaker_failure_threshold",
        category="agent",
        setting_type="int",
        default=5,
        description="Consecutive backend->agent failures before the circuit opens",
        min_value=1,
        max_value=50,
    ),
    SettingDefinition(
        key="agent.circuit_breaker_cooldown_seconds",
        category="agent",
        setting_type="int",
        default=30,
        description="Seconds the circuit stays open before a probe is allowed",
        min_value=5,
        max_value=600,
    ),
    # ── Reservations ──
    SettingDefinition(
        key="reservations.default_ttl_minutes",
        category="reservations",
        setting_type="int",
        default=60,
        description="Default run TTL if not specified",
        min_value=1,
        max_value=1440,
    ),
    SettingDefinition(
        key="reservations.max_ttl_minutes",
        category="reservations",
        setting_type="int",
        default=180,
        description="Maximum allowed TTL (prevents accidental long locks)",
        min_value=1,
        max_value=1440,
    ),
    SettingDefinition(
        key="reservations.default_heartbeat_timeout_sec",
        category="reservations",
        setting_type="int",
        default=120,
        description="Default heartbeat timeout",
        min_value=30,
        max_value=600,
    ),
    SettingDefinition(
        key="reservations.claim_ttl_seconds",
        category="reservations",
        setting_type="int",
        default=120,
        description="How long a worker device claim stays valid without release",
        env_var="GRIDFLEET_RESERVATION_CLAIM_TTL_SECONDS",
        min_value=10,
        max_value=3600,
    ),
    SettingDefinition(
        key="reservations.reaper_interval_sec",
        category="reservations",
        setting_type="int",
        default=15,
        description="How often the stale run reaper runs",
        env_var="GRIDFLEET_RUN_REAPER_INTERVAL_SEC",
        min_value=5,
        max_value=300,
    ),
    # ── Data Retention ──
    SettingDefinition(
        key="retention.sessions_days",
        category="retention",
        setting_type="int",
        default=14,
        description="Delete completed sessions older than N days",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.audit_log_days",
        category="retention",
        setting_type="int",
        default=180,
        description="Delete config audit log entries older than N days",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.device_events_days",
        category="retention",
        setting_type="int",
        default=90,
        description="Delete device incident events older than N days",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.host_resource_telemetry_hours",
        category="retention",
        setting_type="int",
        default=24,
        description="Delete host resource telemetry older than N hours",
        min_value=1,
        max_value=720,
    ),
    SettingDefinition(
        key="retention.capacity_snapshots_days",
        category="retention",
        setting_type="int",
        default=30,
        description="Delete fleet capacity snapshots older than N days",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.cleanup_interval_hours",
        category="retention",
        setting_type="int",
        default=1,
        description="How often the data cleanup task runs",
        min_value=1,
        max_value=168,
    ),
]

SETTINGS_REGISTRY: dict[str, SettingDefinition] = {d.key: d for d in _DEFINITIONS}


def _copy_default(value: SettingValue) -> SettingValue:
    return copy.deepcopy(value)


def _parse_bool(raw: str, env_var: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {env_var}: {raw!r}")


def _parse_env_value(definition: SettingDefinition, raw: str) -> SettingValue:
    if definition.setting_type == "int":
        return int(raw)
    if definition.setting_type == "float":
        return float(raw)
    if definition.setting_type == "bool":
        return _parse_bool(raw, definition.env_var or definition.key)
    if definition.setting_type == "json":
        return json.loads(raw)
    return raw


def resolve_default(definition: SettingDefinition) -> SettingValue:
    """Resolve the default value for a setting.

    Default values are owned by this registry. If a setting exposes an env var,
    parse it directly from the environment; otherwise use the registry default.
    """
    if definition.env_var:
        raw = os.getenv(definition.env_var)
        if raw is not None:
            return _parse_env_value(definition, raw)
    return _copy_default(definition.default)
