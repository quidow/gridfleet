import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.test_run import RunState


class DeviceRequirement(BaseModel):
    pack_id: str
    platform_id: str
    os_version: str | None = None
    count: int | None = Field(default=None, ge=1)
    allocation: Literal["all_available"] | None = None
    min_count: int | None = Field(default=None, ge=1)
    tags: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_allocation(self) -> "DeviceRequirement":
        if self.allocation == "all_available":
            if self.count is not None:
                raise ValueError("count cannot be provided when allocation is all_available")
            if self.min_count is None:
                self.min_count = 1
            return self

        if self.min_count is not None:
            raise ValueError("min_count can only be provided when allocation is all_available")
        if self.count is None:
            self.count = 1
        return self


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    requirements: list[DeviceRequirement]
    ttl_minutes: int | None = None
    heartbeat_timeout_sec: int | None = None
    created_by: str | None = None


class RunPreparationFailureReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    source: str = "ci_preparation"


class ReservedDeviceInfo(BaseModel):
    device_id: str
    identity_value: str
    connection_target: str | None = None
    pack_id: str
    platform_id: str
    platform_label: str | None = None
    os_version: str
    host_ip: str | None = None
    excluded: bool = False
    exclusion_reason: str | None = None
    excluded_at: str | None = None


class SessionCounts(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    passed: int = 0
    failed: int = 0
    error: int = 0
    running: int = 0
    total: int = 0

    @classmethod
    def from_status_map(cls, status_map: dict[str, int]) -> "SessionCounts":
        return cls(
            passed=status_map.get("passed", 0),
            failed=status_map.get("failed", 0),
            error=status_map.get("error", 0),
            running=status_map.get("running", 0),
            total=sum(status_map.values()),
        )


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    state: RunState
    requirements: list[dict[str, Any]]
    ttl_minutes: int
    heartbeat_timeout_sec: int
    reserved_devices: list[ReservedDeviceInfo] | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_by: str | None = None
    last_heartbeat: datetime | None = None
    session_counts: SessionCounts = SessionCounts()


class RunDetail(RunRead):
    devices: list[ReservedDeviceInfo] = []


class RunListRead(BaseModel):
    items: list[RunRead]
    total: int | None = None
    limit: int
    offset: int | None = None
    next_cursor: str | None = None
    prev_cursor: str | None = None


class RunCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    state: RunState
    devices: list[ReservedDeviceInfo]
    grid_url: str
    ttl_minutes: int
    heartbeat_timeout_sec: int
    created_at: datetime


class HeartbeatResponse(BaseModel):
    state: RunState
    last_heartbeat: datetime
