from __future__ import annotations

from typing import Any
from uuid import UUID  # noqa: TC003 - Pydantic resolves this field annotation at runtime.

from pydantic import BaseModel


class AppiumReconfigureRequest(BaseModel):
    accepting_new_sessions: bool = True
    stop_pending: bool = False
    grid_run_id: UUID | None = None


class AppiumStartRequest(BaseModel):
    connection_target: str
    port: int
    grid_url: str
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
    pack_id: str
    platform_id: str
    appium_platform_name: str | None = None
    workaround_env: dict[str, str] | None = None
    insecure_features: list[str] = []
    grid_slots: list[str] = ["native"]
    lifecycle_actions: list[dict[str, Any]] = []
    connection_behavior: dict[str, Any] = {}


class AppiumStopRequest(BaseModel):
    port: int
