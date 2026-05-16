from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventSeverity = Literal["info", "success", "warning", "critical", "neutral"]
ALL_SEVERITIES: frozenset[EventSeverity] = frozenset({"info", "success", "warning", "critical", "neutral"})


@dataclass(frozen=True)
class PublicEventDefinition:
    name: str
    category: str
    description: str
    default_severity: EventSeverity
    allowed_severities: frozenset[EventSeverity]
    typical_data_fields: tuple[str, ...] = ()


EVENT_CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "device_and_node_lifecycle": "Device And Node Lifecycle",
    "host_and_discovery": "Host And Discovery",
    "sessions_and_runs": "Sessions And Runs",
    "operations_and_settings": "Operations And Settings",
}


PUBLIC_EVENT_CATALOG: tuple[PublicEventDefinition, ...] = (
    PublicEventDefinition(
        name="device.operational_state_changed",
        category="device_and_node_lifecycle",
        description="Device operational state changed.",
        default_severity="info",
        allowed_severities=ALL_SEVERITIES,
        typical_data_fields=(
            "device_id",
            "device_name",
            "old_operational_state",
            "new_operational_state",
            "reason",
        ),
    ),
    PublicEventDefinition(
        name="device.hold_changed",
        category="device_and_node_lifecycle",
        description="Device hold changed.",
        default_severity="info",
        allowed_severities=frozenset({"info", "neutral"}),
        typical_data_fields=("device_id", "device_name", "old_hold", "new_hold", "reason"),
    ),
    PublicEventDefinition(
        name="device.verification.updated",
        category="device_and_node_lifecycle",
        description="Verification job progress or completion changed.",
        default_severity="info",
        allowed_severities=frozenset({"info", "success", "warning", "critical"}),
        typical_data_fields=(
            "job_id",
            "status",
            "current_stage",
            "current_stage_status",
            "detail",
            "device_id",
        ),
    ),
    PublicEventDefinition(
        name="device.hardware_health_changed",
        category="device_and_node_lifecycle",
        description="Hardware telemetry changed a device into warning or critical state.",
        default_severity="warning",
        allowed_severities=frozenset({"warning", "critical", "success"}),
        typical_data_fields=(
            "device_id",
            "device_name",
            "old_status",
            "new_status",
            "battery_level_percent",
            "battery_temperature_c",
            "charging_state",
            "reported_at",
        ),
    ),
    PublicEventDefinition(
        name="node.state_changed",
        category="device_and_node_lifecycle",
        description="Managed Appium node state changed.",
        default_severity="info",
        allowed_severities=frozenset({"info", "success", "warning"}),
        typical_data_fields=("device_id", "device_name", "old_state", "new_state", "port"),
    ),
    PublicEventDefinition(
        name="node.crash",
        category="device_and_node_lifecycle",
        description="Managed Appium node crashed or restart failed.",
        default_severity="critical",
        allowed_severities=frozenset({"critical", "warning"}),
        typical_data_fields=("device_id", "device_name", "error", "will_restart"),
    ),
    PublicEventDefinition(
        name="device.crashed",
        category="device_and_node_lifecycle",
        description=(
            "Device-level crash signal. Fires whenever a node_crash incident is persisted; "
            "distinct from node.crash, which is per-Appium-process."
        ),
        default_severity="critical",
        allowed_severities=frozenset({"critical", "warning"}),
        typical_data_fields=(
            "device_id",
            "device_name",
            "source",
            "reason",
            "will_restart",
            "process",
        ),
    ),
    PublicEventDefinition(
        name="device.health_changed",
        category="device_and_node_lifecycle",
        description="Aggregate device health flipped between healthy, unhealthy, or unknown.",
        default_severity="info",
        allowed_severities=frozenset({"info", "success", "warning"}),
        typical_data_fields=("device_id", "healthy", "summary"),
    ),
    PublicEventDefinition(
        name="config.updated",
        category="device_and_node_lifecycle",
        description="Device config changed.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral"}),
        typical_data_fields=("device_id", "device_name", "changed_by"),
    ),
    PublicEventDefinition(
        name="test_data.updated",
        category="device_and_node_lifecycle",
        description="Operator-attached device test_data changed.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral"}),
        typical_data_fields=("device_id", "device_name", "changed_by"),
    ),
    PublicEventDefinition(
        name="host.registered",
        category="host_and_discovery",
        description="Host registered or re-registered with the manager.",
        default_severity="success",
        allowed_severities=frozenset({"success", "info"}),
        typical_data_fields=("host_id", "hostname", "status"),
    ),
    PublicEventDefinition(
        name="host.status_changed",
        category="host_and_discovery",
        description="Host online, offline, or approval status changed.",
        default_severity="info",
        allowed_severities=frozenset({"info", "success", "warning", "critical"}),
        typical_data_fields=("host_id", "hostname", "old_status", "new_status"),
    ),
    PublicEventDefinition(
        name="host.heartbeat_lost",
        category="host_and_discovery",
        description="Host missed enough heartbeats to be considered lost.",
        default_severity="critical",
        allowed_severities=frozenset({"critical", "warning"}),
        typical_data_fields=("host_id", "hostname", "missed_count"),
    ),
    PublicEventDefinition(
        name="host.discovery_completed",
        category="host_and_discovery",
        description="Host-scoped discovery/import pass completed.",
        default_severity="info",
        allowed_severities=frozenset({"info"}),
        typical_data_fields=("host_id", "hostname", "new_devices", "removed_identity_values"),
    ),
    PublicEventDefinition(
        name="host.circuit_breaker.opened",
        category="host_and_discovery",
        description="Backend stopped calling a host agent temporarily after repeated failures.",
        default_severity="critical",
        allowed_severities=frozenset({"critical", "warning"}),
        typical_data_fields=("host", "consecutive_failures", "cooldown_seconds", "last_error"),
    ),
    PublicEventDefinition(
        name="host.circuit_breaker.closed",
        category="host_and_discovery",
        description="Backend resumed agent calls for a host after a successful probe.",
        default_severity="success",
        allowed_severities=frozenset({"success"}),
        typical_data_fields=("host",),
    ),
    PublicEventDefinition(
        name="session.started",
        category="sessions_and_runs",
        description="Grid session started on a tracked device.",
        default_severity="info",
        allowed_severities=frozenset({"info"}),
        typical_data_fields=(
            "session_id",
            "device_id",
            "device_name",
            "test_name",
            "run_id",
            "requested_pack_id",
            "requested_platform_id",
            "requested_device_type",
            "requested_connection_type",
            "requested_capabilities",
        ),
    ),
    PublicEventDefinition(
        name="session.ended",
        category="sessions_and_runs",
        description="Tracked Grid session ended.",
        default_severity="info",
        allowed_severities=frozenset({"info", "success", "warning", "critical"}),
        typical_data_fields=(
            "session_id",
            "device_id",
            "status",
            "requested_pack_id",
            "requested_platform_id",
            "requested_device_type",
            "requested_connection_type",
            "requested_capabilities",
            "error_type",
            "error_message",
        ),
    ),
    PublicEventDefinition(
        name="run.created",
        category="sessions_and_runs",
        description="Run reservation created.",
        default_severity="info",
        allowed_severities=frozenset({"info"}),
        typical_data_fields=("run_id", "name", "device_count", "created_by"),
    ),
    PublicEventDefinition(
        name="run.active",
        category="sessions_and_runs",
        description="Run moved into active state.",
        default_severity="info",
        allowed_severities=frozenset({"info"}),
        typical_data_fields=("run_id", "name"),
    ),
    PublicEventDefinition(
        name="run.completed",
        category="sessions_and_runs",
        description="Run completed and devices were released.",
        default_severity="success",
        allowed_severities=frozenset({"success", "warning"}),
        typical_data_fields=("run_id", "name", "duration"),
    ),
    PublicEventDefinition(
        name="run.cancelled",
        category="sessions_and_runs",
        description="Run was cancelled or force released.",
        default_severity="warning",
        allowed_severities=frozenset({"warning", "info"}),
        typical_data_fields=("run_id", "name"),
    ),
    PublicEventDefinition(
        name="run.expired",
        category="sessions_and_runs",
        description="Run expired because TTL or heartbeat budget was exceeded.",
        default_severity="critical",
        allowed_severities=frozenset({"critical", "warning"}),
        typical_data_fields=("run_id", "name", "reason"),
    ),
    PublicEventDefinition(
        name="device_group.updated",
        category="operations_and_settings",
        description="Device group was created, updated, or deleted.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral", "info"}),
        typical_data_fields=("group_id", "action"),
    ),
    PublicEventDefinition(
        name="device_group.members_changed",
        category="operations_and_settings",
        description="Static device group membership changed.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral", "info"}),
        typical_data_fields=("group_id", "added", "removed"),
    ),
    PublicEventDefinition(
        name="bulk.operation_completed",
        category="operations_and_settings",
        description="Bulk or group operation completed.",
        default_severity="success",
        allowed_severities=frozenset({"success", "warning", "critical"}),
        typical_data_fields=("operation", "total", "succeeded", "failed"),
    ),
    PublicEventDefinition(
        name="settings.changed",
        category="operations_and_settings",
        description="Settings values changed.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral", "info"}),
        typical_data_fields=("key", "value", "keys", "reset", "reset_all"),
    ),
    PublicEventDefinition(
        name="system.cleanup_completed",
        category="operations_and_settings",
        description="Retention cleanup loop completed a pass.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral", "warning"}),
        typical_data_fields=(
            "sessions_deleted",
            "audit_entries_deleted",
            "device_events_deleted",
            "host_resource_samples_deleted",
        ),
    ),
    PublicEventDefinition(
        name="webhook.test",
        category="operations_and_settings",
        description="Synthetic webhook test event was published.",
        default_severity="neutral",
        allowed_severities=frozenset({"neutral"}),
        typical_data_fields=("webhook_id", "webhook_name", "message"),
    ),
    PublicEventDefinition(
        name="pack_feature.degraded",
        category="operations_and_settings",
        description="A driver pack feature transitioned to a not-ok state on a host.",
        default_severity="warning",
        allowed_severities=frozenset({"warning", "critical"}),
        typical_data_fields=("host_id", "pack_id", "feature_id", "ok", "detail"),
    ),
    PublicEventDefinition(
        name="pack_feature.recovered",
        category="operations_and_settings",
        description="A driver pack feature transitioned back to ok on a host.",
        default_severity="success",
        allowed_severities=frozenset({"success"}),
        typical_data_fields=("host_id", "pack_id", "feature_id", "ok", "detail"),
    ),
)

PUBLIC_EVENT_NAMES: tuple[str, ...] = tuple(event.name for event in PUBLIC_EVENT_CATALOG)
PUBLIC_EVENT_NAME_SET = frozenset(PUBLIC_EVENT_NAMES)
DEFAULT_TOAST_EVENT_NAMES: tuple[str, ...] = (
    "node.crash",
    "host.heartbeat_lost",
    "device.operational_state_changed",
    "device.hold_changed",
    "device.hardware_health_changed",
    "run.expired",
)

_EVENT_INDEX: dict[str, PublicEventDefinition] = {event.name: event for event in PUBLIC_EVENT_CATALOG}


def default_severity_for(event_type: str) -> EventSeverity:
    return _EVENT_INDEX[event_type].default_severity


def allowed_severities_for(event_type: str) -> frozenset[EventSeverity]:
    return _EVENT_INDEX[event_type].allowed_severities


def normalize_public_event_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        if item not in PUBLIC_EVENT_NAME_SET or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def validate_public_event_names(value: list[str]) -> list[str]:
    invalid = [item for item in value if item not in PUBLIC_EVENT_NAME_SET]
    if invalid:
        invalid_display = ", ".join(sorted(dict.fromkeys(invalid)))
        raise ValueError(f"Unknown event type(s): {invalid_display}")
    return normalize_public_event_names(value)
