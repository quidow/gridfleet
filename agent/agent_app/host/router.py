"""HTTP routes for host-level health and telemetry."""

from __future__ import annotations

import platform
from typing import Any

from fastapi import APIRouter

from agent_app import __version__
from agent_app.appium import appium_mgr
from agent_app.host.capabilities import get_capabilities_snapshot
from agent_app.host.telemetry import get_host_telemetry
from agent_app.host.version_guidance import get_version_guidance

router = APIRouter(prefix="/agent", tags=["host"])


@router.get("/health", summary="Agent process health, capabilities, and version guidance")
async def health() -> dict[str, Any]:
    capabilities = get_capabilities_snapshot()
    payload: dict[str, Any] = {
        "status": "ok",
        "hostname": platform.node(),
        "os_type": platform.system().lower(),
        "version": __version__,
        "missing_prerequisites": capabilities.get("missing_prerequisites", []),
        "capabilities": capabilities,
    }
    payload["appium_processes"] = appium_mgr.process_snapshot()
    payload["version_guidance"] = get_version_guidance().to_payload()
    return payload


@router.get("/host/telemetry", summary="Snapshot of host CPU/memory/disk telemetry")
async def host_telemetry() -> dict[str, Any]:
    return await get_host_telemetry()
