"""HTTP routes for ``/agent/pack/*``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, status

from agent_app.pack.adapter_dispatch import dispatch_feature_action
from agent_app.pack.dependencies import (  # noqa: TC001 - FastAPI resolves these at runtime
    DesiredPlatformDep,
    HostIdDep,
    OptionalAdapterRegistryDep,
    PackStateLoopDep,
)
from agent_app.pack.discovery import enumerate_pack_candidates, pack_device_properties
from agent_app.pack.dispatch import (
    adapter_health_check,
    adapter_lifecycle_action,
    adapter_normalize_device,
    adapter_telemetry,
)
from agent_app.pack.schemas import (
    FeatureActionRequest,
    NormalizeDeviceRequest,
    NormalizeDeviceResponse,
    _FeatureActionContext,
)

router = APIRouter(prefix="/agent/pack", tags=["pack"])


@router.get("/devices", summary="Pack-aware enumeration of candidate devices")
async def pack_devices(
    pack_state_loop: PackStateLoopDep,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
) -> dict[str, Any]:
    desired = pack_state_loop.latest_desired_packs if pack_state_loop else None
    return await enumerate_pack_candidates(
        desired,
        adapter_registry=adapter_registry,
        host_id=host_id,
    )


@router.get(
    "/devices/{connection_target}/properties",
    summary="Pack-shaped device properties via adapter",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Pack device not found"}},
)
async def pack_device_properties_route(
    connection_target: str,
    pack_state_loop: PackStateLoopDep,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
    pack_id: str = Query(...),
) -> dict[str, Any]:
    desired = pack_state_loop.latest_desired_packs if pack_state_loop else None
    data = await pack_device_properties(
        connection_target,
        pack_id,
        desired,
        adapter_registry=adapter_registry,
        host_id=host_id,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Pack device {connection_target} not found")
    return data


@router.get(
    "/devices/{connection_target}/health",
    summary="Pack-shaped device health check via adapter",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Unknown desired pack platform"}},
)
async def pack_device_health_route(
    connection_target: str,
    platform: DesiredPlatformDep,
    adapter_registry: OptionalAdapterRegistryDep,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    device_type: str = Query(...),
    connection_type: str | None = Query(None),
    ip_address: str | None = Query(None),
    allow_boot: bool = Query(False),
    headless: bool = Query(True),
    ip_ping_timeout_sec: float | None = Query(None),
    ip_ping_count: int | None = Query(None),
) -> dict[str, Any]:
    _platform_def, release = platform
    if adapter_registry is not None:
        payload = await adapter_health_check(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            identity_value=connection_target,
            allow_boot=allow_boot,
            platform_id=platform_id,
            device_type=device_type,
            connection_type=connection_type,
            ip_address=ip_address,
            ip_ping_timeout_sec=ip_ping_timeout_sec,
            ip_ping_count=ip_ping_count,
        )
        if payload is not None:
            return payload
    return {
        "healthy": None,
        "checks": [
            {
                "check_id": "adapter_unavailable",
                "ok": False,
                "message": f"Adapter not loaded for pack {pack_id}:{platform_id}",
            }
        ],
    }


@router.get(
    "/devices/{connection_target}/telemetry",
    summary="Pack-shaped device telemetry via adapter",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Device not found"}},
)
async def pack_device_telemetry_route(
    connection_target: str,
    platform: DesiredPlatformDep,
    adapter_registry: OptionalAdapterRegistryDep,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    device_type: str = Query(...),
    connection_type: str | None = Query(None),
    ip_address: str | None = Query(None),
) -> dict[str, Any]:
    _platform_def, release = platform
    telemetry = (
        await adapter_telemetry(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            identity_value=connection_target,
            connection_target=connection_target,
        )
        if adapter_registry is not None
        else None
    )
    if telemetry is None:
        raise HTTPException(status_code=404, detail=f"Device {connection_target} not found or not connected")
    return telemetry


@router.post(
    "/devices/{connection_target}/lifecycle/{action}",
    summary="Dispatch a lifecycle action through the adapter",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Unknown desired pack platform"}},
)
async def pack_device_lifecycle_route(
    connection_target: str,
    action: str,
    platform: DesiredPlatformDep,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    args: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    _platform_def, release = platform
    if adapter_registry is not None:
        payload = await adapter_lifecycle_action(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            host_id=host_id,
            identity_value=connection_target,
            action=action,
            args=args,
        )
        if payload is not None:
            return payload
    return {
        "success": False,
        "detail": f"Adapter not loaded for pack {pack_id}:{platform_id}",
    }


@router.post(
    "/features/{feature_id}/actions/{action_id}",
    summary="Dispatch a feature action through the adapter",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No adapter loaded"}},
)
async def feature_action_route(
    feature_id: str,
    action_id: str,
    body: FeatureActionRequest,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
) -> dict[str, Any]:
    adapter = adapter_registry.get_current(body.pack_id) if adapter_registry is not None else None
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {body.pack_id!r}")

    ctx = _FeatureActionContext(
        host_id=host_id,
        device_identity_value=body.device_identity_value or "",
    )
    result = await dispatch_feature_action(adapter, feature_id, action_id, body.args, ctx)
    return {"ok": result.ok, "detail": result.detail, "data": result.data}


@router.post(
    "/devices/normalize",
    response_model=NormalizeDeviceResponse,
    summary="Normalize raw device input into pack canonical form",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No adapter loaded"}},
)
async def normalize_device_route(
    req: NormalizeDeviceRequest,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
) -> dict[str, Any]:
    if adapter_registry is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")

    result = await adapter_normalize_device(
        adapter_registry=adapter_registry,
        pack_id=req.pack_id,
        pack_release=req.pack_release,
        host_id=host_id,
        platform_id=req.platform_id,
        raw_input=req.raw_input,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")
    return result
