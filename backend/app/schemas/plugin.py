import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PluginCreate(BaseModel):
    name: str
    version: str
    source: str
    package: str | None = None
    enabled: bool = True
    notes: str = ""


class PluginUpdate(BaseModel):
    name: str | None = None
    version: str | None = None
    source: str | None = None
    package: str | None = None
    enabled: bool | None = None
    notes: str | None = None


class PluginRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    version: str
    source: str
    package: str | None
    enabled: bool
    notes: str
    created_at: datetime
    updated_at: datetime


class PluginSyncResult(BaseModel):
    installed: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class FleetPluginSyncResult(BaseModel):
    total_hosts: int
    online_hosts: list[uuid.UUID] = Field(default_factory=list)
    synced_hosts: list[uuid.UUID] = Field(default_factory=list)
    failed_hosts: list[uuid.UUID] = Field(default_factory=list)
    skipped_hosts: list[uuid.UUID] = Field(default_factory=list)


class HostPluginStatus(BaseModel):
    name: str
    required_version: str
    installed_version: str | None
    status: str
    enabled: bool
