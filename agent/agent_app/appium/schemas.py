from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_app.pack.constants import PACK_ID_PATTERN, PLATFORM_ID_PATTERN


class NodeRefreshResponse(BaseModel):
    accepted: bool = True


class AppiumStartRequest(BaseModel):
    connection_target: str = Field(min_length=1, max_length=512)
    port: int = Field(ge=1024, le=65535)
    extra_caps: dict[str, Any] | None = None
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None
    allocated_caps: dict[str, Any] | None = None
    device_type: str | None = None
    ip_address: str | None = None
    session_override: bool = True
    headless: bool = True
    pack_id: str = Field(min_length=1, pattern=PACK_ID_PATTERN)
    platform_id: str = Field(min_length=1, pattern=PLATFORM_ID_PATTERN)
    # Release the backend derived this launch payload from; None on payloads
    # from backends that predate the field. The start gate requires it to match
    # the pack-state cache's desired release exactly.
    pack_release: str | None = None

    appium_platform_name: str | None = None
    appium_env: dict[str, str] | None = None
    insecure_features: list[str] = []
    lifecycle_actions: list[dict[str, Any]] = []
    connection_behavior: dict[str, Any] = {}


class AppiumStatusResponse(BaseModel):
    """Status snapshot for a managed Appium process. Adapter-specific fields permitted."""

    model_config = ConfigDict(extra="allow")

    port: int = Field(ge=1024, le=65535)
    running: bool
    pid: int | None = None
    appium_status: dict[str, Any] | None = None


class AppiumLogsResponse(BaseModel):
    port: int = Field(ge=1024, le=65535)
    lines: list[str]
    count: int = Field(ge=0)
