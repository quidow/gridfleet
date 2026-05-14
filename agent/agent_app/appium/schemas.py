from __future__ import annotations

from typing import Any
from uuid import UUID  # noqa: TC003 - Pydantic resolves this field annotation at runtime.

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


class AppiumReconfigureRequest(BaseModel):
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None


class AppiumStartRequest(BaseModel):
    connection_target: str = Field(min_length=1, max_length=512)
    port: int = Field(ge=1024, le=65535)
    grid_url: str = Field(min_length=1)
    plugins: list[str] | None = None
    extra_caps: dict[str, Any] | None = None
    stereotype_caps: dict[str, Any] | None = None
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None
    allocated_caps: dict[str, Any] | None = None
    device_type: str | None = None
    ip_address: str | None = None
    session_override: bool = True
    headless: bool = True
    pack_id: str = Field(min_length=1, pattern=_PACK_ID_STRUCTURAL_PATTERN)
    platform_id: str = Field(min_length=1, pattern=_PLATFORM_ID_PATTERN)

    @field_validator("pack_id")
    @classmethod
    def pack_id_no_dot_segments(cls, v: str) -> str:
        return _validate_no_dot_segments(v)

    appium_platform_name: str | None = None
    workaround_env: dict[str, str] | None = None
    insecure_features: list[str] = []
    grid_slots: list[str] = ["native"]
    lifecycle_actions: list[dict[str, Any]] = []
    connection_behavior: dict[str, Any] = {}


class AppiumStopRequest(BaseModel):
    port: int = Field(ge=1024, le=65535)


class AppiumStartResponse(BaseModel):
    pid: int = Field(ge=1)
    port: int = Field(ge=1024, le=65535)
    connection_target: str = Field(min_length=1)


class AppiumReconfigureResponse(BaseModel):
    port: int = Field(ge=1024, le=65535)
    accepting_new_sessions: bool
    stop_pending: bool
    grid_run_id: str | None = None


class AppiumStopResponse(BaseModel):
    stopped: bool
    port: int = Field(ge=1024, le=65535)


class AppiumStatusResponse(BaseModel):
    """Shape varies — accept extras so OpenAPI captures the typed core only."""

    model_config = ConfigDict(extra="allow")

    port: int = Field(ge=1024, le=65535)


class AppiumLogsResponse(BaseModel):
    port: int = Field(ge=1024, le=65535)
    lines: list[str]
    count: int = Field(ge=0)
