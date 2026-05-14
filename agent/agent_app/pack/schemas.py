"""Pack-router DTOs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Single-segment identifiers: alphanumeric, underscores, dots, hyphens — no slashes.
# Used for platform_id values like "android", "ios", "android_mobile", "android-emulator".
_PLATFORM_ID_PATTERN = r"^[A-Za-z0-9_.\-]+$"

# Structural pattern for pack IDs: slash-separated segments of valid chars.
# Enforces no leading/trailing slashes and no consecutive slashes.
# Traversal segments ("." or "..") are blocked by the field_validator below.
_PACK_ID_STRUCTURAL_PATTERN = r"^[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*$"


def _validate_no_dot_segments(v: str) -> str:
    """Reject any segment that is exactly '.' or '..' (path traversal guard)."""
    for segment in v.split("/"):
        if segment in {".", ".."}:
            msg = f"pack_id segment {segment!r} is not allowed"
            raise ValueError(msg)
    return v


class FeatureActionRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=_PACK_ID_STRUCTURAL_PATTERN)
    args: dict[str, Any] = {}
    device_identity_value: str | None = None

    @field_validator("pack_id")
    @classmethod
    def pack_id_no_dot_segments(cls, v: str) -> str:
        return _validate_no_dot_segments(v)


class NormalizeDeviceRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=_PACK_ID_STRUCTURAL_PATTERN)
    pack_release: str = Field(min_length=1)
    platform_id: str = Field(min_length=1, pattern=_PLATFORM_ID_PATTERN)

    @field_validator("pack_id")
    @classmethod
    def pack_id_no_dot_segments(cls, v: str) -> str:
        return _validate_no_dot_segments(v)

    raw_input: dict[str, Any]


class NormalizeDeviceResponse(BaseModel):
    identity_scheme: str
    identity_scope: str
    identity_value: str
    connection_target: str
    ip_address: str
    device_type: str
    connection_type: str
    os_version: str
    manufacturer: str = ""
    model: str = ""
    model_number: str = ""
    software_versions: dict[str, str] = Field(default_factory=dict)
    field_errors: list[dict[str, str]]


class PackDeviceCandidate(BaseModel):
    """A single entry in ``GET /agent/pack/devices``. Adapters return arbitrary keys."""

    model_config = ConfigDict(extra="allow")


class PackDevicesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidates: list[PackDeviceCandidate] = Field(default_factory=list)


class PackDevicePropertiesResponse(BaseModel):
    """Adapter-defined property bag. Schema is intentionally open."""

    model_config = ConfigDict(extra="allow")


class HealthCheckResult(BaseModel):
    """One check in a pack device's health response."""

    model_config = ConfigDict(extra="allow")

    check_id: str
    ok: bool
    message: str | None = None


class PackDeviceHealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    healthy: bool | None
    checks: list[HealthCheckResult] = Field(default_factory=list)


class PackDeviceTelemetryResponse(BaseModel):
    """Adapter telemetry blob."""

    model_config = ConfigDict(extra="allow")


class PackDeviceLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool
    detail: str | None = None


class FeatureActionResponse(BaseModel):
    ok: bool
    detail: str | None = None
    data: dict[str, Any] | None = None


class _FeatureActionContext:
    """Concrete LifecycleContext used when dispatching feature actions."""

    __slots__ = ("device_identity_value", "host_id")

    def __init__(self, host_id: str, device_identity_value: str) -> None:
        self.host_id = host_id
        self.device_identity_value = device_identity_value
