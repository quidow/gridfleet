from typing import Any

from pydantic import BaseModel, Field

from app.devices.models import DeviceHold, DeviceOperationalState


class GridRegistryDeviceRead(BaseModel):
    id: str
    identity_value: str
    connection_target: str | None = None
    name: str
    platform_id: str
    operational_state: DeviceOperationalState
    hold: DeviceHold | None = None
    node_state: str | None = None
    node_port: int | None = None


class GridRegistryRead(BaseModel):
    device_count: int
    devices: list[GridRegistryDeviceRead] = Field(default_factory=list)


class GridStatusRead(BaseModel):
    # Selenium Grid status is an external payload; keep it flexible inside a typed envelope.
    grid: dict[str, Any]
    registry: GridRegistryRead
    active_sessions: int
    queue_size: int


class GridQueueRead(BaseModel):
    queue_size: int
    requests: list[Any] = Field(default_factory=list)
