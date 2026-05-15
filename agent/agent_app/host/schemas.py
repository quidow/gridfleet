"""Response schemas for ``/agent/health`` and ``/agent/host/telemetry``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Agent health snapshot returned by ``GET /agent/health``."""

    model_config = ConfigDict(extra="allow")

    status: str
    hostname: str
    os_type: str
    version: str
    registered: bool
    missing_prerequisites: list[str]
    capabilities: dict[str, Any]
    appium_processes: dict[str, Any]
    version_guidance: dict[str, Any]


class HostTelemetryResponse(BaseModel):
    """Host CPU/memory/disk snapshot. Fields vary by platform — accept extras."""

    model_config = ConfigDict(extra="allow")
