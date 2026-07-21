import enum
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import AppError
from app.devices.group_keys import GroupKey
from app.devices.models import ConnectionType, DeviceType, GroupType
from app.devices.schemas.filters import DeviceGroupFilters

SCHEMA_VERSION = 2

UNSUPPORTED_SCHEMA_VERSION_MESSAGE = f"unsupported portability schema version; expected {SCHEMA_VERSION}"


class UnsupportedSchemaVersionError(AppError):
    """Raised while parsing a bundle whose ``schema_version`` this build cannot read.

    Deliberately not a ``ValueError``: Pydantic converts ``ValueError`` into a field
    error, which would bury the version verdict inside a 422 alongside unrelated
    complaints about retired keys. Propagating an ``AppError`` instead lets the
    version gate answer first, with the documented message and status.
    """

    status_code = 400
    code = "UNSUPPORTED_SCHEMA_VERSION"

    def __init__(self) -> None:
        super().__init__(UNSUPPORTED_SCHEMA_VERSION_MESSAGE)


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

    @field_validator("static_groups")
    @classmethod
    def _dedupe_static_groups(cls, value: list[GroupKey]) -> list[GroupKey]:
        """Collapse repeated keys, keeping first-seen order.

        ``device_group_memberships`` is unique on ``(group_id, device_id)``, so a
        bundle listing the same key twice would stage two identical rows and fail
        the importer's membership commit on that constraint. Membership is
        set-valued — a repeat carries no extra meaning — so collapse it at the
        edge rather than making every consumer of ``static_groups`` defend
        against it. ``_plan_static_memberships`` and the ``MembershipSkip``
        generator both iterate this list raw and both rely on this.
        """
        return list(dict.fromkeys(value))


class ExportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    exported_at: datetime
    source_instance: str | None = None
    groups: list[ExportedDeviceGroup] = Field(default_factory=list)
    devices: list[ExportedDevice]

    @model_validator(mode="before")
    @classmethod
    def _gate_schema_version(cls, data: object) -> object:
        """Reject a foreign schema version before field validation runs.

        A real v1 bundle carries a ``tags`` map on every device, which this model's
        ``extra="forbid"`` would otherwise report as a pile of per-device extra-input
        errors — never mentioning the version, which is the only thing the operator
        can act on. Running in ``before`` mode puts the version verdict first.
        """
        if not isinstance(data, Mapping):
            return data
        raw = data.get("schema_version")
        if raw is None:
            return data
        try:
            version = int(raw)
        except TypeError, ValueError:
            return data  # Let field validation report the type error.
        if version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError
        return data


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


class MembershipSkip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    group_key: str
    reason: str


class ImportCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: list[ImportCommitCreatedRow]
    skipped: list[ImportCommitSkippedRow]
    failed: list[ImportCommitFailedRow]
    memberships_skipped: list[MembershipSkip] = Field(default_factory=list)
