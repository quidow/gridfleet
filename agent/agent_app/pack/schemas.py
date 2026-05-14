"""Pack-router DTOs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.\-]+$"


class FeatureActionRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    args: dict[str, Any] = {}
    device_identity_value: str | None = None


class NormalizeDeviceRequest(BaseModel):
    pack_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    pack_release: str = Field(min_length=1)
    platform_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
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


class _FeatureActionContext:
    """Concrete LifecycleContext used when dispatching feature actions."""

    __slots__ = ("device_identity_value", "host_id")

    def __init__(self, host_id: str, device_identity_value: str) -> None:
        self.host_id = host_id
        self.device_identity_value = device_identity_value
