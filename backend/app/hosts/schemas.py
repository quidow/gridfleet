import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.devices.models import ConnectionType, DeviceType
from app.devices.schemas.device import DeviceRead
from app.hosts.models import HostStatus, OSType
from app.hosts.service_versioning import AgentVersionStatus


class HostCreate(BaseModel):
    hostname: str
    ip: str
    os_type: OSType
    agent_port: int | None = None


class HostStatusPush(BaseModel):
    """Consolidated agent status push. Sections stay flexible dicts inside a
    typed envelope; extra keys are ignored for forward compatibility."""

    model_config = ConfigDict(extra="ignore")

    host_id: uuid.UUID
    agent_version: str | None = None
    capabilities: dict[str, Any] | None = None
    missing_prerequisites: list[str] | None = None
    appium_processes: dict[str, Any] = Field(default_factory=dict)
    host_telemetry: dict[str, Any] | None = None
    packs: dict[str, Any] | None = None
    node_health: dict[str, Any] | None = None
    device_health: dict[str, Any] | None = None
    device_telemetry: dict[str, Any] | None = None
    device_properties: dict[str, Any] | None = None


class HostHardwareInfo(BaseModel):
    os_version: str | None = None
    kernel_version: str | None = None
    cpu_arch: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    total_memory_mb: int | None = None
    total_disk_gb: int | None = None


class HostRegister(BaseModel):
    hostname: str
    ip: str
    os_type: OSType
    agent_port: int | None = None
    # capabilities is retained solely as the 426 orchestration-contract gate
    # input; it is not persisted at registration (the status push owns the
    # capabilities column). agent_version is push-owned and no longer accepted.
    capabilities: dict[str, Any] | None = None
    host_info: HostHardwareInfo | None = None


class HostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hostname: str
    ip: str
    os_type: OSType
    agent_port: int
    status: HostStatus
    agent_version: str | None = None
    required_agent_version: str | None = None
    recommended_agent_version: str | None = None
    agent_update_available: bool = False
    agent_version_status: AgentVersionStatus = AgentVersionStatus.disabled
    capabilities: dict[str, Any] | None = None
    tool_env: dict[str, str] | None = None
    missing_prerequisites: list[str] = []
    last_heartbeat: datetime | None
    created_at: datetime
    os_version: str | None = None
    kernel_version: str | None = None
    cpu_arch: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    total_memory_mb: int | None = None
    total_disk_gb: int | None = None


class HostDetail(HostRead):
    devices: list[DeviceRead] = []


class HostCircuitBreakerRead(BaseModel):
    status: str
    consecutive_failures: int
    cooldown_seconds: float
    retry_after_seconds: float | None = None
    probe_in_flight: bool
    last_error: str | None = None


class HostDiagnosticsNodeRead(BaseModel):
    port: int
    pid: int | None = None
    connection_target: str | None = None
    platform_id: str | None = None
    managed: bool = False
    node_id: uuid.UUID | None = None
    node_state: str | None = None
    device_id: uuid.UUID | None = None
    device_name: str | None = None


class HostAppiumProcessesRead(BaseModel):
    reported_at: datetime | None = None
    running_nodes: list[HostDiagnosticsNodeRead] = []


class HostRecoveryEventRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    device_name: str
    event_type: str
    process: str | None = None
    kind: str
    sequence: int | None = None
    port: int | None = None
    pid: int | None = None
    attempt: int | None = None
    delay_sec: int | None = None
    exit_code: int | None = None
    will_restart: bool | None = None
    occurred_at: datetime
    recorded_at: datetime


class HostDiagnosticsRead(BaseModel):
    host_id: uuid.UUID
    circuit_breaker: HostCircuitBreakerRead
    appium_processes: HostAppiumProcessesRead
    recent_recovery_events: list[HostRecoveryEventRead] = []


class HostResourceSampleRead(BaseModel):
    timestamp: datetime
    cpu_percent: float | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    disk_percent: float | None = None


class HostResourceTelemetryResponse(BaseModel):
    samples: list[HostResourceSampleRead] = []
    latest_recorded_at: datetime | None = None
    window_start: datetime
    window_end: datetime
    bucket_minutes: int


class ToolEntry(BaseModel):
    name: str
    version: str | None = None
    description: str


class HostToolStatusRead(BaseModel):
    host: dict[str, ToolEntry]
    packs: dict[str, list[ToolEntry]]


class IntakeCandidateBase(BaseModel):
    pack_id: str
    platform_id: str
    platform_label: str | None = None
    identity_scheme: str
    identity_scope: str
    identity_value: str
    connection_target: str | None = None
    name: str
    os_version: str
    manufacturer: str = ""
    model: str = ""
    model_number: str = ""
    software_versions: dict[str, Any] | None = None
    detected_properties: dict[str, Any] | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    ip_address: str | None = None


class DiscoveredDevice(IntakeCandidateBase):
    readiness_state: str = "verification_required"
    missing_setup_fields: list[str] = []
    can_verify_now: bool = True


class DiscoveryResult(BaseModel):
    new_devices: list[DiscoveredDevice] = []
    removed_identity_values: list[str] = []
    updated_devices: list[DiscoveredDevice] = []


class DiscoveryConfirm(BaseModel):
    add_identity_values: list[str] = []
    remove_identity_values: list[str] = []


class DiscoveryConfirmResult(BaseModel):
    added: list[str] = []
    removed: list[str] = []
    updated: list[str] = []
    added_devices: list[DeviceRead] = []


class IntakeCandidateRead(IntakeCandidateBase):
    already_registered: bool = False
    registered_device_id: uuid.UUID | None = None


class HostEventEntry(BaseModel):
    event_id: str
    type: str
    ts: datetime
    data: dict[str, Any]


class HostEventsPage(BaseModel):
    events: list[HostEventEntry]
    total: int
    has_more: bool = False


class HostToolEnvRead(BaseModel):
    env: dict[str, str]


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENV_KEY_LEN = 256
_MAX_ENV_VALUE_LEN = 4096


class HostToolEnvUpdate(BaseModel):
    env: dict[str, str]

    @field_validator("env")
    @classmethod
    def validate_env_entries(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not _ENV_KEY_RE.match(key):
                raise ValueError(f"invalid env var name (must be POSIX-safe): {key!r}")
            if len(key) > _MAX_ENV_KEY_LEN:
                raise ValueError(f"env var name exceeds {_MAX_ENV_KEY_LEN} chars: {key[:32]}...")
            if len(value) > _MAX_ENV_VALUE_LEN:
                raise ValueError(f"env var value exceeds {_MAX_ENV_VALUE_LEN} chars for key: {key}")
            if "\x00" in value:
                raise ValueError(f"env var value must not contain null bytes: {key}")
        return v
