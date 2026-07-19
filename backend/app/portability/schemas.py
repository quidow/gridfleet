import enum
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.devices.group_keys import GroupKey
from app.devices.models import ConnectionType, DeviceType, GroupType
from app.devices.schemas.filters import DeviceGroupFilters

SCHEMA_VERSION = 2


class OriginalHost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    host_id: uuid.UUID | None = None


class ExportedDeviceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: GroupKey
    name: str
    description: str | None = None
    group_type: GroupType
    filters: DeviceGroupFilters | None = None


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
    static_groups: list[GroupKey] = Field(default_factory=list)
    device_config: dict[str, Any] = Field(default_factory=dict)
    test_data: dict[str, Any] = Field(default_factory=dict)
    original_host: OriginalHost


class ExportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    exported_at: datetime
    source_instance: str | None = None
    groups: list[ExportedDeviceGroup] = Field(default_factory=list)
    devices: list[ExportedDevice]


class ImportRowStatus(enum.StrEnum):
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
    status: ImportRowStatus
    host_suggestion: HostSuggestion | None = None
    issues: list[str] = Field(default_factory=list)


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
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


class InventoryColumn(enum.StrEnum):
    ID = "id"
    NAME = "name"
    REVIEW_REQUIRED = "review_required"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    HOST_ID = "host.id"
    HOST_HOSTNAME = "host.hostname"
    PACK_ID = "pack_id"
    PLATFORM_ID = "platform_id"
    IDENTITY_SCHEME = "identity.scheme"
    IDENTITY_SCOPE = "identity.scope"
    IDENTITY_VALUE = "identity.value"
    DEVICE_TYPE = "device_type"
    CONNECTION_TYPE = "connection_type"
    CONNECTION_TARGET = "connection_target"
    OS_VERSION = "os_version"
    MANUFACTURER = "manufacturer"
    MODEL = "model"
    MODEL_NUMBER = "model_number"
    SOFTWARE_VERSIONS = "software_versions"
    OPERATIONAL_STATE = "operational_state"
    DEVICE_CONFIG = "device_config"
    TEST_DATA = "test_data"
    HARDWARE_BATTERY_LEVEL = "hardware.battery_level_percent"
    HARDWARE_BATTERY_TEMPERATURE = "hardware.battery_temperature_c"
    HARDWARE_CHARGING_STATE = "hardware.charging_state"
    HARDWARE_HEALTH_STATUS = "hardware.health_status"
    HARDWARE_TELEMETRY_REPORTED_AT = "hardware.telemetry_reported_at"
    VERIFICATION_VERIFIED_AT = "verification.verified_at"
    VERIFICATION_SESSION_VIABILITY_STATUS = "verification.session_viability_status"
    VERIFICATION_DEVICE_CHECKS_HEALTHY = "verification.device_checks_healthy"
    VERIFICATION_DEVICE_CHECKS_CHECKED_AT = "verification.device_checks_checked_at"


_VALID_VALUES = {c.value for c in InventoryColumn}


def parse_columns_param(raw: str | None) -> list[InventoryColumn]:
    if not raw:
        return list(InventoryColumn)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return list(InventoryColumn)
    invalid = [t for t in tokens if t not in _VALID_VALUES]
    if invalid:
        raise ValueError(f"unknown columns: {invalid}")
    seen: set[str] = set()
    deduped: list[InventoryColumn] = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(InventoryColumn(t))
    return deduped


class InventoryFormat(enum.StrEnum):
    CSV = "csv"
    JSON = "json"
