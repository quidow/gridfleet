"""HTTP routes for host-level health and telemetry."""

from __future__ import annotations

import platform
from typing import Any

from fastapi import APIRouter, status

from agent_app import __version__
from agent_app.appium.dependencies import AppiumMgrDep  # noqa: TC001 - FastAPI resolves at runtime
from agent_app.host.dependencies import (  # noqa: TC001 - FastAPI resolves at runtime
    CapabilitiesDep,
    HostTelemetryDep,
    VersionGuidanceDep,
)
from agent_app.host.schemas import HealthResponse, HostTelemetryResponse

router = APIRouter(prefix="/agent", tags=["host"])


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Agent process health, capabilities, and version guidance",
)
async def health(
    capabilities: CapabilitiesDep,
    version_guidance: VersionGuidanceDep,
    mgr: AppiumMgrDep,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "hostname": platform.node(),
        "os_type": platform.system().lower(),
        "version": __version__,
        "missing_prerequisites": capabilities.get("missing_prerequisites", []),
        "capabilities": capabilities,
        "appium_processes": mgr.process_snapshot(),
        "version_guidance": version_guidance,
    }


@router.get(
    "/host/telemetry",
    response_model=HostTelemetryResponse,
    status_code=status.HTTP_200_OK,
    summary="Snapshot of host CPU/memory/disk telemetry",
)
async def host_telemetry(payload: HostTelemetryDep) -> dict[str, Any]:
    return payload
