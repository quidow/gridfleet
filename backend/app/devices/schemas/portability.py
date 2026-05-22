import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.devices.models import ConnectionType, DeviceType

SCHEMA_VERSION = 1


class OriginalHost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    host_id: uuid.UUID | None = None


class ExportedDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str
    platform_id: str
    identity_scheme: str
    identity_scope: Literal["global", "host"]
    identity_value: str
    name: str
    device_type: DeviceType
    connection_type: ConnectionType
    connection_target: str | None = None
    auto_manage: bool = True
    tags: dict[str, str] = Field(default_factory=dict)
    device_config: dict[str, Any] = Field(default_factory=dict)
    test_data: dict[str, Any] = Field(default_factory=dict)
    original_host: OriginalHost


class ExportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    exported_at: datetime
    source_instance: str | None = None
    devices: list[ExportedDevice]


class ImportRowStatus:
    VALID_NEW = "valid_new"
    CONFLICT_SKIP = "conflict_skip"
    DUPLICATE_IN_BUNDLE = "duplicate_in_bundle"
    INVALID = "invalid"


class HostSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    hostname: str


class ImportPreviewRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    device: ExportedDevice
    status: Literal["valid_new", "conflict_skip", "duplicate_in_bundle", "invalid"]
    host_suggestion: HostSuggestion | None = None
    issues: list[str] = Field(default_factory=list)


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source_instance: str | None = None
    exported_at: datetime
    bundle_hash: str
    available_hosts: list[HostSuggestion]
    rows: list[ImportPreviewRow]


class ImportMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    target_host_id: uuid.UUID


class ImportCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: ExportBundle
    bundle_hash: str
    mappings: list[ImportMapping]


class ImportCommitCreatedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    device_id: uuid.UUID


class ImportCommitSkippedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    reason: str


class ImportCommitFailedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    reason: str


class ImportCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: list[ImportCommitCreatedRow]
    skipped: list[ImportCommitSkippedRow]
    failed: list[ImportCommitFailedRow]
