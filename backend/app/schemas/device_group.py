import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.device import DeviceRead
from app.schemas.device_filters import DeviceGroupFilters


class DeviceGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    group_type: str = "static"
    filters: DeviceGroupFilters | None = None


class DeviceGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    filters: DeviceGroupFilters | None = None


class DeviceGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    group_type: str
    filters: DeviceGroupFilters | None
    device_count: int = 0
    created_at: datetime
    updated_at: datetime


class DeviceGroupDetail(DeviceGroupRead):
    devices: list[DeviceRead] = Field(default_factory=list)


class GroupMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_ids: list[uuid.UUID]
