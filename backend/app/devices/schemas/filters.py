import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.devices.group_keys import GroupKey
from app.devices.models import ConnectionType, DeviceType, HardwareHealthStatus
from app.devices.schemas.device import HardwareTelemetryState

ChipStatus = Literal["available", "busy", "offline", "maintenance", "verifying"]
DeviceSortBy = Literal[
    "name",
    "platform",
    "device_type",
    "connection_type",
    "os_version",
    "os_version_display",
    "host",
    "status",
    "operational_state",
    "created_at",
]
DeviceSortDir = Literal["asc", "desc"]
HealthVerdictFilter = Literal["ok", "warn", "failed", "unknown"]


class DeviceGroupFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str | None = None
    platform_id: str | None = None
    status: ChipStatus | None = None
    reserved: bool | None = None
    host_id: uuid.UUID | None = None
    identity_value: str | None = None
    connection_target: str | None = None
    device_type: DeviceType | None = None
    connection_type: ConnectionType | None = None
    os_version: str | None = None
    os_version_display: str | None = None
    hardware_health_status: HardwareHealthStatus | None = None
    hardware_telemetry_state: HardwareTelemetryState | None = None
    needs_attention: bool | None = None
    tags: dict[str, str] | None = None
    member_of: list[GroupKey] = Field(default_factory=list)

    @model_serializer(mode="plain")
    def serialize(self) -> dict[str, object]:
        data: dict[str, object] = {}
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if value is None:
                continue
            if field_name == "member_of" and not value:
                continue
            data[field_name] = value
        return data


class DeviceQueryFilters(DeviceGroupFilters):
    search: str | None = None
    device_health: HealthVerdictFilter | None = None
    node_health: HealthVerdictFilter | None = None
    viability: HealthVerdictFilter | None = None
    sort_by: DeviceSortBy = "created_at"
    sort_dir: DeviceSortDir = "desc"
    groups: list[GroupKey] = Field(default_factory=list)
