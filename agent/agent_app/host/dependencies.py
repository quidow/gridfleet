"""FastAPI dependencies for ``/agent/health`` and ``/agent/host/*``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from agent_app.host.capabilities import get_capabilities_snapshot
from agent_app.host.telemetry import get_host_telemetry
from agent_app.host.version_guidance import get_version_guidance


def get_capabilities_snapshot_dep() -> dict[str, Any]:
    return get_capabilities_snapshot()


async def get_host_telemetry_dep() -> dict[str, Any]:
    return await get_host_telemetry()


def get_version_guidance_payload() -> dict[str, Any]:
    return get_version_guidance().to_payload()


def get_registered_flag(request: Request) -> bool:
    identity = getattr(request.app.state, "host_identity", None)
    if identity is None:
        return False
    return bool(identity.get())


CapabilitiesDep = Annotated[dict[str, Any], Depends(get_capabilities_snapshot_dep)]
HostTelemetryDep = Annotated[dict[str, Any], Depends(get_host_telemetry_dep)]
RegisteredFlagDep = Annotated[bool, Depends(get_registered_flag)]
VersionGuidanceDep = Annotated[dict[str, Any], Depends(get_version_guidance_payload)]
