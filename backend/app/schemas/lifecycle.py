import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.device_event import DeviceEventType
from app.schemas.device import DeviceLifecyclePolicySummaryState


class LifecycleIncidentRead(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    device_name: str
    device_identity_value: str
    platform_id: str
    event_type: DeviceEventType
    label: str
    summary_state: DeviceLifecyclePolicySummaryState
    reason: str | None = None
    detail: str | None = None
    source: str | None = None
    run_id: uuid.UUID | None = None
    run_name: str | None = None
    backoff_until: datetime | None = None
    created_at: datetime


class LifecycleIncidentListRead(BaseModel):
    items: list[LifecycleIncidentRead]
    limit: int
    next_cursor: str | None = None
    prev_cursor: str | None = None
