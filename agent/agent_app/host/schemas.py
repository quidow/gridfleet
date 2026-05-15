"""Response schemas for ``/agent/health`` and ``/agent/host/telemetry``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Agent health snapshot returned by ``GET /agent/health``."""

    model_config = ConfigDict(extra="forbid")

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
    """Host CPU/memory/disk snapshot. Known fields are typed; rest live in extras."""

    model_config = ConfigDict(extra="forbid")

    recorded_at: str | None = None
    cpu_percent: float | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    disk_percent: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
