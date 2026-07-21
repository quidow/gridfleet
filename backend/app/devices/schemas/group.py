import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.devices.group_keys import GroupKey
from app.devices.models.group import GroupType
from app.devices.schemas.device import DeviceRead
from app.devices.schemas.filters import DeviceGroupFilters


class DeviceGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: GroupKey
    name: str
    description: str | None = None
    group_type: GroupType = GroupType.static
    filters: DeviceGroupFilters | None = None


class DeviceGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    filters: DeviceGroupFilters | None = None


class _DeviceGroupReadBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    description: str | None
    group_type: GroupType
    filters: DeviceGroupFilters | None
    created_at: datetime
    updated_at: datetime


class DeviceGroupRead(_DeviceGroupReadBase):
    device_count: int = 0


class DeviceGroupMutationRead(_DeviceGroupReadBase):
    device_count: int | None = None


class DeviceGroupDetail(DeviceGroupRead):
    devices: list[DeviceRead] = Field(default_factory=list)


class GroupMembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_ids: list[uuid.UUID]
