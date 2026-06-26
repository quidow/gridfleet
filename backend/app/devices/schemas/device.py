import enum
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, computed_field, model_validator

from app.appium_nodes.services.effective_state import compute_effective_state
from app.core.timeutil import now_utc
from app.devices.models import (
    ConnectionType,
    DeviceOperationalState,
    DeviceType,
    HardwareChargingState,
    HardwareHealthStatus,
)
from app.devices.readiness_types import ReadinessState
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY
from app.sessions.viability_types import SessionViabilityCheckedBy

DeviceTags = dict[str, str]


class DesiredNodeState(enum.StrEnum):
    running = "running"
    stopped = "stopped"


EffectiveNodeState = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "blocked",
    "error",
]


class DeviceLifecyclePolicySummaryState(enum.StrEnum):
    idle = "idle"
    deferred_stop = "deferred_stop"
    backoff = "backoff"
    excluded = "excluded"
    suppressed = "suppressed"
    recoverable = "recoverable"


class DeviceVerificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str
    platform_id: str
    identity_scheme: str | None = None
    identity_scope: str | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    name: str
    os_version: str = "unknown"
    os_version_display: str | None = None
    host_id: uuid.UUID
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None


class DeviceVerificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str | None = None
    platform_id: str | None = None
    identity_scheme: str | None = None
    identity_scope: str | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    name: str | None = None
    os_version: str | None = None
    os_version_display: str | None = None
    host_id: uuid.UUID
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None
    replace_device_config: bool | None = None


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    connection_target: str | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None
    replace_device_config: bool | None = None


class AppiumNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    port: int
    pid: int | None
    container_id: str | None
    active_connection_target: str | None
    started_at: datetime
    desired_state: DesiredNodeState
    desired_port: int | None = None
    transition_token: uuid.UUID | None = None
    transition_deadline: datetime | None = None
    last_observed_at: datetime | None = None
    health_running: bool | None = None
    health_state: str | None = None
    lifecycle_policy_state: dict[str, Any] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_state(self) -> EffectiveNodeState:
        return compute_effective_state(
            pid=self.pid,
            desired_state=self.desired_state.value,
            health_running=self.health_running,
            health_state=self.health_state,
            transition_token=self.transition_token,
            transition_deadline=self.transition_deadline,
            lifecycle_policy_state=self.lifecycle_policy_state,
            now=now_utc(),
        )


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: str
    test_name: str | None
    started_at: datetime
    ended_at: datetime | None
    status: SessionStatus
    requested_capabilities: dict[str, Any] | None = None
    actual_capabilities: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    run_id: uuid.UUID | None = None
    is_probe: bool = False
    probe_checked_by: str | None = None

    @model_validator(mode="after")
    def _derive_probe_fields(self) -> SessionRead:
        is_probe = self.test_name == PROBE_TEST_NAME
        probe_checked_by: str | None = None
        if is_probe and isinstance(self.requested_capabilities, dict):
            raw = self.requested_capabilities.get(PROBE_CHECKED_BY_CAP_KEY)
            if isinstance(raw, str):
                probe_checked_by = raw
        self.is_probe = is_probe
        self.probe_checked_by = probe_checked_by
        return self


class SessionOutcomeHeatmapRow(BaseModel):
    timestamp: datetime
    status: SessionStatus


class DeviceReservationRead(BaseModel):
    run_id: uuid.UUID
    run_name: str
    run_state: str
    excluded: bool = False
    exclusion_reason: str | None = None
    excluded_until: datetime | None = None
    cooldown_remaining_sec: int | None = None
    cooldown_count: int = 0
    cooldown_escalated: bool = False


class DeviceLifecyclePolicySummaryRead(BaseModel):
    state: DeviceLifecyclePolicySummaryState
    label: str
    detail: str | None = None
    backoff_until: datetime | None = None
    maintenance_reason: str | None = None


HealthVerdictStatus = Literal["ok", "warn", "failed", "unknown"]


class HealthVerdictRead(BaseModel):
    status: HealthVerdictStatus
    detail: str | None = None
    checked_at: str | None = None


class DeviceHealthSummaryRead(BaseModel):
    device: HealthVerdictRead
    node: HealthVerdictRead
    viability: HealthVerdictRead
    overall: HealthVerdictStatus


class DeviceConfigRead(RootModel[dict[str, Any]]):
    pass


class ConfigAuditEntryRead(BaseModel):
    id: uuid.UUID
    previous_config: dict[str, Any] | None = None
    new_config: dict[str, Any]
    changed_by: str | None = None
    changed_at: datetime


class DeviceHealthNodeRead(BaseModel):
    running: bool
    port: int | None = None
    state: str | None = None


class SessionViabilityRead(BaseModel):
    status: Literal["passed", "failed"] | None = None
    last_attempted_at: str | None = None
    last_succeeded_at: str | None = None
    error: str | None = None
    checked_by: SessionViabilityCheckedBy | None = None


class DeviceHealthRead(BaseModel):
    platform: str
    node: DeviceHealthNodeRead
    device_checks: dict[str, Any]
    session_viability: SessionViabilityRead | None = None
    # Lifecycle policy payload is intentionally open while the policy engine evolves.
    lifecycle_policy: dict[str, Any]
    healthy: bool


class DeviceIntentSummaryRead(BaseModel):
    source: str
    axis: str
    run_id: uuid.UUID | None = None
    payload: dict[str, Any]
    expires_at: datetime | None = None


class DeviceOrchestrationRead(BaseModel):
    intents: list[DeviceIntentSummaryRead]
    derived: dict[str, Any]


class HardwareTelemetryState(enum.StrEnum):
    unknown = "unknown"
    fresh = "fresh"
    stale = "stale"
    unsupported = "unsupported"


class UnavailableReason(enum.StrEnum):
    busy = "busy"
    verifying = "verifying"
    maintenance = "maintenance"
    offline = "offline"
    reserved = "reserved"
    # Warm soft-gate park (Stage 2): node is up but not accepting new sessions
    # (cooldown). Distinct from ``offline`` (node down / crashed).
    cooldown = "cooldown"
    # Node transition window (Stage 4 / P6): the node is healthy and the device is
    # still ``available``, but its Appium process is mid-transition — a restart is in
    # flight (``transition_token`` set) or its routable target is not yet settled
    # (``active_connection_target`` missing) — so the allocator's node-viability gate
    # refuses it. Distinct from ``offline`` (process down) and ``cooldown`` (warm but
    # soft-gated): the node stays warm and returns to allocatable within seconds, with
    # NO operational_state flip. Read-side only; operational_state never models this.
    transitioning = "transitioning"


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pack_id: str
    platform_id: str
    platform_label: str | None = None
    identity_scheme: str
    identity_scope: str
    identity_value: str
    connection_target: str | None
    name: str
    os_version: str
    os_version_display: str | None = None
    host_id: uuid.UUID
    operational_state: DeviceOperationalState
    is_reserved: bool = False
    allocatable: bool
    unavailable_reason: UnavailableReason | None = None
    tags: DeviceTags | None
    manufacturer: str | None
    model: str | None
    model_number: str | None
    software_versions: dict[str, Any] | None
    device_type: DeviceType
    connection_type: ConnectionType
    ip_address: str | None
    device_config: dict[str, Any] | None = None
    battery_level_percent: int | None
    battery_temperature_c: float | None
    charging_state: HardwareChargingState | None
    hardware_health_status: HardwareHealthStatus
    hardware_telemetry_reported_at: datetime | None
    hardware_telemetry_state: HardwareTelemetryState
    readiness_state: ReadinessState
    missing_setup_fields: list[str]
    verified_at: datetime | None
    reservation: DeviceReservationRead | None = None
    lifecycle_policy_summary: DeviceLifecyclePolicySummaryRead
    needs_attention: bool
    health_summary: DeviceHealthSummaryRead
    emulator_state: str | None = None
    blocked_reason: str | None = None
    review_required: bool = False
    review_reason: str | None = None
    review_set_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DeviceDetail(DeviceRead):
    appium_node: AppiumNodeRead | None = None
    orchestration: DeviceOrchestrationRead | None = None


class SessionDetail(SessionRead):
    device_id: uuid.UUID | None = None
    device_name: str | None = None
    device_pack_id: str | None = None
    device_platform_id: str | None = None
    device_platform_label: str | None = None

    @classmethod
    def from_session(cls, session: Session, *, device_platform_label: str | None = None) -> SessionDetail:
        device = session.device
        return cls(
            id=session.id,
            session_id=session.session_id,
            test_name=session.test_name,
            started_at=session.started_at,
            ended_at=session.ended_at,
            status=session.status,
            requested_capabilities=session.requested_capabilities,
            actual_capabilities=session.actual_capabilities,
            error_type=session.error_type,
            error_message=session.error_message,
            run_id=session.run_id,
            device_id=session.device_id,
            device_name=device.name if device else None,
            device_pack_id=device.pack_id if device else None,
            device_platform_id=device.platform_id if device else None,
            device_platform_label=device_platform_label,
        )


class DeviceListPage(BaseModel):
    items: list[DeviceRead]
    total: int
    limit: int
    offset: int


class SessionListRead(BaseModel):
    items: list[SessionDetail]
    total: int | None = None
    limit: int
    offset: int | None = None
    next_cursor: str | None = None
    prev_cursor: str | None = None


class SessionStatusUpdate(BaseModel):
    status: SessionStatus


class SessionKillResult(BaseModel):
    """Outcome of an operator kill: the row is always terminalized; ``terminated``
    reports whether the Appium DELETE actually succeeded."""

    terminated: bool
    session: SessionRead


# --- Bulk operation schemas ---


class BulkDeviceIds(BaseModel):
    device_ids: list[uuid.UUID]


class BulkTagsUpdate(BaseModel):
    device_ids: list[uuid.UUID]
    tags: DeviceTags
    merge: bool = True


class BulkOperationResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    errors: dict[str, str] = Field(default_factory=dict)
