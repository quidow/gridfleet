import enum
import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.appium_node import NodeState
from app.models.device import (
    ConnectionType,
    DeviceHold,
    DeviceOperationalState,
    DeviceType,
    HardwareChargingState,
    HardwareHealthStatus,
)
from app.models.session import Session, SessionStatus

DeviceTags = dict[str, str]


class DeviceCreate(BaseModel):
    name: str
    pack_id: str
    platform_id: str
    identity_scheme: str | None = None
    identity_scope: str | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    os_version: str
    host_id: uuid.UUID
    device_type: DeviceType
    connection_type: ConnectionType
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    pack_id: str | None = None
    platform_id: str | None = None
    identity_scheme: str | None = None
    identity_scope: str | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    os_version: str | None = None
    host_id: uuid.UUID | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None


class DeviceLifecyclePolicySummaryState(enum.StrEnum):
    idle = "idle"
    deferred_stop = "deferred_stop"
    backoff = "backoff"
    excluded = "excluded"
    suppressed = "suppressed"
    recoverable = "recoverable"
    manual = "manual"


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
    host_id: uuid.UUID
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    auto_manage: bool = True
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
    host_id: uuid.UUID
    tags: DeviceTags | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_number: str | None = None
    software_versions: dict[str, Any] | None = None
    auto_manage: bool | None = None
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
    auto_manage: bool | None = None
    connection_target: str | None = None
    ip_address: str | None = None
    device_config: dict[str, Any] | None = None
    replace_device_config: bool | None = None


class AppiumNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    port: int
    grid_url: str
    pid: int | None
    container_id: str | None
    active_connection_target: str | None
    state: NodeState
    started_at: datetime


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: str
    test_name: str | None
    started_at: datetime
    ended_at: datetime | None
    status: SessionStatus
    requested_pack_id: str | None = None
    requested_platform_id: str | None = None
    requested_device_type: DeviceType | None = None
    requested_connection_type: ConnectionType | None = None
    requested_capabilities: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    run_id: uuid.UUID | None = None


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


class DeviceHealthSummaryRead(BaseModel):
    healthy: bool | None
    summary: str
    last_checked_at: str | None = None


class HardwareTelemetryState(enum.StrEnum):
    unknown = "unknown"
    fresh = "fresh"
    stale = "stale"
    unsupported = "unsupported"


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
    host_id: uuid.UUID
    operational_state: DeviceOperationalState
    hold: DeviceHold | None
    tags: DeviceTags | None
    manufacturer: str | None
    model: str | None
    model_number: str | None
    software_versions: dict[str, Any] | None
    auto_manage: bool
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
    readiness_state: str
    missing_setup_fields: list[str]
    verified_at: datetime | None
    reservation: DeviceReservationRead | None = None
    lifecycle_policy_summary: DeviceLifecyclePolicySummaryRead
    needs_attention: bool
    health_summary: DeviceHealthSummaryRead
    emulator_state: str | None = None
    blocked_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class DeviceListRead(BaseModel):
    items: list[DeviceRead]
    total: int
    limit: int
    offset: int


class DeviceDetail(DeviceRead):
    appium_node: AppiumNodeRead | None = None
    sessions: list[SessionRead] = Field(default_factory=list)


class DeviceVerificationJobRead(BaseModel):
    job_id: str
    status: str
    current_stage: str | None = None
    current_stage_status: str | None = None
    detail: str | None = None
    error: str | None = None
    device_id: uuid.UUID | None = None
    started_at: str
    finished_at: str | None = None


class SessionDetail(SessionRead):
    device_id: uuid.UUID | None = None
    device_name: str | None = None
    device_pack_id: str | None = None
    device_platform_id: str | None = None
    device_platform_label: str | None = None

    @classmethod
    def from_session(cls, session: Session, *, device_platform_label: str | None = None) -> "SessionDetail":
        device = session.device
        return cls(
            id=session.id,
            session_id=session.session_id,
            test_name=session.test_name,
            started_at=session.started_at,
            ended_at=session.ended_at,
            status=session.status,
            requested_pack_id=session.requested_pack_id,
            requested_platform_id=session.requested_platform_id,
            requested_device_type=session.requested_device_type,
            requested_connection_type=session.requested_connection_type,
            requested_capabilities=session.requested_capabilities,
            error_type=session.error_type,
            error_message=session.error_message,
            run_id=session.run_id,
            device_id=session.device_id,
            device_name=device.name if device else None,
            device_pack_id=device.pack_id if device else None,
            device_platform_id=device.platform_id if device else None,
            device_platform_label=device_platform_label,
        )


class SessionListRead(BaseModel):
    items: list[SessionDetail]
    total: int | None = None
    limit: int
    offset: int | None = None
    next_cursor: str | None = None
    prev_cursor: str | None = None


class SessionStatusUpdate(BaseModel):
    status: SessionStatus


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    test_name: str | None = None
    device_id: uuid.UUID | None = None
    connection_target: str | None = None
    status: SessionStatus = SessionStatus.running
    requested_pack_id: str | None = None
    requested_platform_id: str | None = None
    requested_device_type: DeviceType | None = None
    requested_connection_type: ConnectionType | None = None
    requested_capabilities: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    run_id: uuid.UUID | None = None

    @field_validator("requested_capabilities")
    @classmethod
    def validate_requested_capabilities_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        size = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if size > 32 * 1024:
            raise ValueError("requested_capabilities must serialize to 32 KB or less")
        return value


# --- Bulk operation schemas ---


class BulkDeviceIds(BaseModel):
    device_ids: list[uuid.UUID]


class BulkAutoManageUpdate(BaseModel):
    device_ids: list[uuid.UUID]
    auto_manage: bool


class BulkTagsUpdate(BaseModel):
    device_ids: list[uuid.UUID]
    tags: DeviceTags
    merge: bool = True


class BulkMaintenanceEnter(BaseModel):
    device_ids: list[uuid.UUID]
    drain: bool = False


class BulkOperationResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    errors: dict[str, str] = Field(default_factory=dict)
