from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class GridQueueRequestRead(BaseModel):
    """One waiting new-session ticket, Selenium-queue-shaped (camelCase keys)."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(alias="requestId")
    capabilities: dict[str, Any]
    request_timestamp: str = Field(alias="requestTimestamp")
    # Run attribution comes from the ticket row (the run-scoped /run/{id}
    # endpoint binding), not from capabilities — the gridfleet:run_id cap is
    # retired. None = free session.
    run_id: str | None = Field(default=None, alias="runId")


class GridQueueRead(BaseModel):
    queue_size: int
    requests: list[GridQueueRequestRead] = Field(default_factory=list)
