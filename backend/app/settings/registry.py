"""Registry of all known settings with metadata, types, defaults, and validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    category: str
    setting_type: str  # "int", "float", "string", "bool", "json"
    default: SettingValue
    description: str
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: list[str] | None = field(default=None)
    item_allowed_values: list[str] | None = field(default=None)


# Ordered display names for UI tabs
CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "general": "General",
    "grid": "Appium & Allocation",
    "agent": "Agent",
    "reservations": "Reservations",
    "retention": "Data Retention",
    "device_checks": "Device Checks",
}

_DEFINITIONS: list[SettingDefinition] = [
    # ── General ──
    SettingDefinition(
        key="general.host_offline_after_sec",
        category="general",
        setting_type="int",
        default=45,
        description="Seconds without a status push before a host is marked offline",
        min_value=15,
        max_value=3600,
    ),
    SettingDefinition(
        key="general.node_fail_window_sec",
        category="general",
        setting_type="int",
        default=60,
        description=(
            "How long node health checks must keep failing (wall-clock, across the agent's "
            "30s node probe cycles) before the node is marked offline and restart escalation "
            "runs; 0 = first failure."
        ),
        min_value=0,
        max_value=600,
    ),
    SettingDefinition(
        key="general.device_cooldown_max_sec",
        category="general",
        setting_type="int",
        default=3600,
        description="Maximum run-scoped device cooldown accepted from clients",
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
        min_value=0,
        max_value=100,
    ),
    SettingDefinition(
        key="general.run_failure_escalates_to_maintenance",
        category="general",
        setting_type="bool",
        default=True,
        description=(
            "When a device is escalated out of a run (CI preparation failure, or cooldown threshold exceeded), "
            "true places it into maintenance (manual recovery); false leaves it available. The device is released "
            "from the run regardless of this setting."
        ),
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
        key="general.session_viability_failure_threshold",
        category="general",
        setting_type="int",
        default=3,
        description=(
            "Consecutive session_viability failures required before the manager "
            "parks the device. Tolerates transient Appium session hiccups."
        ),
        min_value=1,
        max_value=20,
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
    SettingDefinition(
        key="general.lifecycle_recovery_review_threshold",
        category="general",
        setting_type="int",
        default=5,
        description=(
            "Consecutive automatic recovery failures before the device is shelved into "
            "``review_required`` state. Once shelved, automated recovery loops skip the device "
            "until an operator action (exit maintenance, restore from run, re-verify, restart node) "
            "clears the flag."
        ),
        min_value=1,
        max_value=100,
    ),
    # ── Device Checks ──
    SettingDefinition(
        key="device_checks.ip_ping.fail_window_sec",
        category="device_checks",
        setting_type="int",
        default=120,
        description=(
            "How long ICMP-ping must keep failing (wall-clock, measured across the agent's "
            "60s device-health probe cycles) before an opted-in device is marked unhealthy. "
            "0 = strict, first miss flips."
        ),
        min_value=0,
        max_value=3600,
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
    SettingDefinition(
        key="device_checks.probe_unanswered.fail_window_sec",
        category="device_checks",
        setting_type="int",
        default=120,
        description=(
            "How long unanswered health probes (agent/adapter error) must persist before a "
            "device is marked unhealthy instead of being silently skipped."
        ),
        min_value=0,
        max_value=3600,
    ),
    SettingDefinition(
        key="device_checks.probe_failed.fail_window_sec",
        category="device_checks",
        setting_type="int",
        default=120,
        description=(
            "How long a manifest-declared debounceable health check (e.g. Roku ECP reachability "
            "on port 8060) may keep failing before the device is marked unhealthy. "
            "0 = strict, first miss flips."
        ),
        min_value=0,
        max_value=3600,
    ),
    # ── Appium & Allocation ──
    SettingDefinition(
        key="grid.queue_timeout_sec",
        category="grid",
        setting_type="int",
        default=300,
        description=(
            "How long a queued new-session request may wait for a device before failing "
            "(must exceed the router's 25s long-poll slice)"
        ),
        min_value=30,
        max_value=3600,
    ),
    SettingDefinition(
        key="grid.session_idle_timeout_sec",
        category="grid",
        setting_type="int",
        default=1800,
        description=(
            "How long a running session may go without reported client activity before the observation sweep "
            "terminates it. Replaces the relay's idle timeout (driver enforcement of newCommandTimeout is "
            "config-dependent), so an abandoned client that crashes without a DELETE cannot pin its device busy "
            "forever. A client appium:newCommandTimeout above this value extends the window per session, up to "
            "grid.session_idle_timeout_ceiling_sec."
        ),
        min_value=60,
        max_value=86400,
    ),
    SettingDefinition(
        key="grid.session_idle_timeout_ceiling_sec",
        category="grid",
        setting_type="int",
        default=7200,
        description=(
            "Hard ceiling on how far a client's appium:newCommandTimeout may extend the idle reap window. "
            "The observation sweep honors an idle contract the client negotiated above "
            "grid.session_idle_timeout_sec, up to this ceiling; newCommandTimeout=0 ('never idle-kill') clamps "
            "here, preserving the zombie-session guarantee. Clients can extend the idle window, never shorten it."
        ),
        min_value=60,
        max_value=86400,
    ),
    SettingDefinition(
        key="grid.session_first_command_grace_sec",
        category="grid",
        setting_type="int",
        default=180,
        description=(
            "How long a running session whose client has never issued a command (NULL last_activity_at) may live "
            "before the observation sweep terminates it. Measured from the allocation claim (started_at), so Appium "
            "session-create time eats into the grace. Bounds abandoned-client zombie sessions that claim a device but "
            "never route any WebDriver traffic, well below the full idle timeout."
        ),
        min_value=30,
        max_value=3600,
    ),
    SettingDefinition(
        key="grid.claim_window_sec",
        category="grid",
        setting_type="int",
        default=120,
        description=(
            "A pending row older than this window is a crash orphan and is failed by the reaper; "
            "a live create is always bounded below it (session_create.effective_create_timeout)."
        ),
        min_value=30,
        max_value=600,
    ),
    SettingDefinition(
        key="appium.port_range_start",
        category="grid",
        setting_type="int",
        default=4723,
        description=(
            "Start of the port range the backend assigns managed Appium nodes from. "
            "Each agent only binds ports inside its own AGENT_APPIUM_PORT_RANGE_* env; "
            "keep this range within every host's env range."
        ),
        min_value=1024,
        max_value=65535,
    ),
    SettingDefinition(
        key="appium.port_range_end",
        category="grid",
        setting_type="int",
        default=4823,
        description=(
            "End of the port range the backend assigns managed Appium nodes from. "
            "Each agent only binds ports inside its own AGENT_APPIUM_PORT_RANGE_* env; "
            "keep this range within every host's env range."
        ),
        min_value=1024,
        max_value=65535,
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
        key="appium_reconciler.restart_window_sec",
        category="grid",
        setting_type="int",
        default=120,
        description=(
            "Wall-clock window (seconds) within which a fresh appium_nodes.restart_requested_at "
            "watermark projects a node as 'restarting' (read-time bounding). Past this window a "
            "still-unsatisfied watermark self-clears at read time — there is no sweep."
        ),
        min_value=30,
        max_value=600,
    ),
    # ── Agent ──
    SettingDefinition(
        key="agent.min_version",
        category="agent",
        setting_type="string",
        default="0.33.0",
        description="Minimum required agent version (empty = no check)",
    ),
    SettingDefinition(
        key="agent.recommended_version",
        category="agent",
        setting_type="string",
        default="",
        description="Recommended agent version shown to operators and agents (empty = no recommendation)",
    ),
    SettingDefinition(
        key="agent.auto_accept_hosts",
        category="agent",
        setting_type="bool",
        default=False,
        description="Auto-accept self-registering hosts (off by default: operators approve hosts manually)",
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
        key="retention.probe_sessions_days",
        category="retention",
        setting_type="int",
        default=7,
        description="Delete probe session rows older than N days (diagnostic; separate from retention.sessions_days)",
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
        key="retention.remediation_log_days",
        category="retention",
        setting_type="int",
        default=30,
        description="Delete device remediation log entries older than N days (escalation-ladder memory)",
        min_value=7,
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
        key="retention.system_events_days",
        category="retention",
        setting_type="int",
        default=30,
        description="Delete system events older than N days",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.test_runs_days",
        category="retention",
        setting_type="int",
        default=30,
        description="Delete terminal test runs older than N days (their device reservations cascade)",
        min_value=1,
        max_value=3650,
    ),
    SettingDefinition(
        key="retention.jobs_days",
        category="retention",
        setting_type="int",
        default=30,
        description="Delete completed or failed durable jobs older than N days",
        min_value=1,
        max_value=3650,
    ),
]

SETTINGS_REGISTRY: dict[str, SettingDefinition] = {d.key: d for d in _DEFINITIONS}


def _copy_default(value: SettingValue) -> SettingValue:
    return copy.deepcopy(value)


def resolve_default(definition: SettingDefinition) -> SettingValue:
    """Default values are owned by this registry; env vars never seed them (WS-4.2)."""
    return _copy_default(definition.default)
