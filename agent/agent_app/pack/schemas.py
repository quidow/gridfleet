"""Pack-router DTOs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_app.pack.constants import PACK_ID_PATTERN, PLATFORM_ID_PATTERN


class FeatureActionRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=PACK_ID_PATTERN)
    args: dict[str, Any] = {}
    device_identity_value: str | None = None


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
    manufacturer: str = ""
    model: str = ""
    model_number: str = ""
    software_versions: dict[str, str] = Field(default_factory=dict)
    field_errors: list[dict[str, str]]


class PackDeviceCandidate(BaseModel):
    """A single entry in ``GET /agent/pack/devices``."""

    model_config = ConfigDict(extra="forbid")

    connection_target: str
    platform_id: str
    device_type: str
    pack_id: str
    pack_release: str
    extras: dict[str, Any] = Field(default_factory=dict)


class PackDevicesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[PackDeviceCandidate] = Field(default_factory=list)


class PackDevicePropertiesResponse(BaseModel):
    """Adapter-defined property bag under a stable response envelope."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    pack_release: str
    properties: dict[str, Any] = Field(default_factory=dict)


class HealthCheckResult(BaseModel):
    """One check in a pack device's health response."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    ok: bool
    message: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class PackDeviceHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool | None
    checks: list[HealthCheckResult] = Field(default_factory=list)


class PackDeviceTelemetryResponse(BaseModel):
    """Adapter telemetry metrics under a stable response envelope."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    pack_release: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class PackDeviceLifecycleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    detail: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


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
