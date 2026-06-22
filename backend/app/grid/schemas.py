from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.appium_nodes.services.effective_state import EffectiveNodeStateValue
from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason


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
    ready: bool
    message: str
    registry: GridRegistryRead
    active_sessions: int
    active_session_ids: list[str]
    running_node_count: int
    queue_size: int
    queued_request_ids: list[str]


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


class GridRouterCounts(BaseModel):
    registered: int
    running: int
    available: int
    busy: int
    verifying: int
    offline: int
    maintenance: int
    # Devices the allocator could serve right now: available ∧ node-viable ∧ accepting
    # ∧ no live session — the same gate as ``allocation._eligible_devices`` (reservation
    # is NOT subtracted; a reserved-but-ready device is still routable to its run). The
    # Router "open" pill renders this, and "not ready" is ``available - eligible``.
    eligible: int
    active_sessions: int
    queue_depth: int


class GridRouterNodeRead(BaseModel):
    device_id: str
    device_name: str
    platform_id: str
    host_id: str | None = None
    host_name: str | None = None
    operational_state: DeviceOperationalState
    node_effective_state: EffectiveNodeStateValue | None = None
    # Why the allocator would refuse this device an arbitrary new session now, or null
    # if it is open. For an ``available`` device this distinguishes the not-ready causes
    # (``transitioning`` / ``cooldown`` / ``reserved``) the Router card surfaces.
    unavailable_reason: UnavailableReason | None = None
    session_id: str | None = None
    session_target: str | None = None
    stereotype: dict[str, Any]


class GridRouterRead(BaseModel):
    # nodes/queue are always populated by the handler, so they are required (not
    # defaulted) — this keeps the generated TS types non-optional for the frontend.
    counts: GridRouterCounts
    nodes: list[GridRouterNodeRead]
    queue: list[GridQueueRequestRead]
