from typing import Any

from pydantic import BaseModel, Field

from app.devices.models import DeviceOperationalState


class GridRegistryDeviceRead(BaseModel):
    id: str
    identity_value: str
    connection_target: str | None = None
    name: str
    platform_id: str
    operational_state: DeviceOperationalState
    node_state: str | None = None
    node_port: int | None = None


class GridRegistryRead(BaseModel):
    device_count: int
    devices: list[GridRegistryDeviceRead] = Field(default_factory=list)


class GridStatusRead(BaseModel):
    # Hub-shaped envelope synthesized from DB state; kept flexible so the frontend's
    # existing grid.value.{ready,message,nodes} readers keep working post-hub-removal.
    grid: dict[str, Any]
    registry: GridRegistryRead
    active_sessions: int
    queue_size: int


class GridQueueRead(BaseModel):
    queue_size: int
    requests: list[Any] = Field(default_factory=list)
