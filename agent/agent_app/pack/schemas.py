"""Pack-router DTOs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_app.pack.constants import PACK_ID_PATTERN, PLATFORM_ID_PATTERN


class NormalizeDeviceRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=PACK_ID_PATTERN)
    pack_release: str = Field(min_length=1)
    platform_id: str = Field(min_length=1, pattern=PLATFORM_ID_PATTERN)
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
    os_version_display: str | None = None
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
    complete_gather: bool = False


class HealthCheckResult(BaseModel):
    """One check in a pack device's health response."""

    model_config = ConfigDict(extra="allow")

    check_id: str
    ok: bool
    message: str | None = None
    debounce: bool = False


class PackDeviceHealthResponse(BaseModel):
    """Pack-shaped health snapshot. Typed core; adapters may add extra fields."""

    model_config = ConfigDict(extra="allow")

    healthy: bool | None
    checks: list[HealthCheckResult] = Field(default_factory=list)
    recommended_action: str | None = None


class PackDeviceLifecycleResponse(BaseModel):
    """Pack lifecycle action outcome. Typed core; adapters may add extra fields."""

    model_config = ConfigDict(extra="allow")

    success: bool
    detail: str | None = None


class DoctorCheckOut(BaseModel):
    check_id: str
    ok: bool
    message: str = ""


class PackDoctorResponse(BaseModel):
    checks: list[DoctorCheckOut] = Field(default_factory=list)
