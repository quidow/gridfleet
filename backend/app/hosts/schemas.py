import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.devices.models import ConnectionType, DeviceType
from app.devices.schemas.device import DeviceRead
from app.hosts.models import HostStatus, OSType
from app.hosts.service_versioning import AgentVersionStatus


class HostCreate(BaseModel):
    hostname: str
    ip: str
    os_type: OSType
    agent_port: int | None = None


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
    agent_version: str | None = None
    capabilities: dict[str, Any] | None = None
    host_info: HostHardwareInfo | None = None


class HostUpdate(BaseModel):
    hostname: str | None = None
    ip: str | None = None
    os_type: OSType | None = None
    agent_port: int | None = None
    status: HostStatus | None = None
    agent_version: str | None = None
    capabilities: dict[str, Any] | None = None


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


class HostToolStatusRead(BaseModel):
    node: str | None = None
    node_provider: str | None = None
    node_error: str | None = None
    go_ios: str | None = None


class DiscoveredDevice(BaseModel):
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


class IntakeCandidateRead(BaseModel):
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
    already_registered: bool = False
    registered_device_id: uuid.UUID | None = None


class ShippedLogLineIngest(BaseModel):
    ts: datetime
    level: str = Field(min_length=1, max_length=16)
    logger_name: str = Field(min_length=1, max_length=255)
    message: str = Field(max_length=16384)
    sequence_no: int = Field(ge=0)


class AgentLogBatchIngest(BaseModel):
    boot_id: uuid.UUID
    lines: list[ShippedLogLineIngest] = Field(default_factory=list, max_length=2000)


class AgentLogIngestResult(BaseModel):
    accepted: int
    deduped: int


class AgentLogLine(BaseModel):
    ts: datetime
    level: str
    logger_name: str
    message: str
    sequence_no: int
    boot_id: uuid.UUID


class AgentLogPage(BaseModel):
    lines: list[AgentLogLine]
    total: int
    has_more: bool


class HostEventEntry(BaseModel):
    event_id: str
    type: str
    ts: datetime
    data: dict[str, Any]


class HostEventsPage(BaseModel):
    events: list[HostEventEntry]
    total: int
    has_more: bool = False
