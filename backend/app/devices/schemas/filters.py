import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.devices.models import ConnectionType, DeviceType, HardwareHealthStatus
from app.devices.schemas.device import HardwareTelemetryState

ChipStatus = Literal["available", "busy", "offline", "maintenance", "reserved", "verifying"]
DeviceSortBy = Literal[
    "name",
    "platform",
    "device_type",
    "connection_type",
    "os_version",
    "host",
    "status",
    "operational_state",
    "hold",
    "created_at",
]
DeviceSortDir = Literal["asc", "desc"]


class DeviceGroupFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str | None = None
    platform_id: str | None = None
    status: ChipStatus | None = None
    host_id: uuid.UUID | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    os_version: str | None = None
    hardware_health_status: HardwareHealthStatus | None = None
    hardware_telemetry_state: HardwareTelemetryState | None = None
    needs_attention: bool | None = None
    tags: dict[str, str] | None = None


class DeviceQueryFilters(DeviceGroupFilters):
    search: str | None = None
    sort_by: DeviceSortBy = "created_at"
    sort_dir: DeviceSortDir = "desc"
