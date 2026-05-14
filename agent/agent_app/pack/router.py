"""HTTP routes for ``/agent/pack/*``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from agent_app.pack.adapter_dispatch import dispatch_feature_action
from agent_app.pack.dependencies import (  # noqa: TC001 - FastAPI resolves these at runtime
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
from agent_app.pack.manifest import resolve_desired_platform

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.schemas import (
    FeatureActionRequest,
    NormalizeDeviceRequest,
    NormalizeDeviceResponse,
    _FeatureActionContext,
)

router = APIRouter(prefix="/agent/pack", tags=["pack"])


def _latest_desired(request: Request) -> list[Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    return list(loop.latest_desired_packs or []) if loop else []


def _release_for_pack(request: Request, pack_id: str) -> str | None:
    for pack in _latest_desired(request):
        if getattr(pack, "id", None) == pack_id:
            return str(getattr(pack, "release", ""))
    return None


@router.get("/devices")
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


@router.get("/devices/{connection_target}/properties")
async def pack_device_properties_route(
    request: Request,
    connection_target: str,
    pack_id: str = Query(...),
) -> dict[str, Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    desired = loop.latest_desired_packs if loop else None
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    host_identity = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else None
    data = await pack_device_properties(
        connection_target,
        pack_id,
        desired,
        adapter_registry=adapter_registry,
        host_id=host_id_value or "",
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"Pack device {connection_target} not found")
    return data


@router.get("/devices/{connection_target}/health")
async def pack_device_health_route(
    request: Request,
    connection_target: str,
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
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    release = _release_for_pack(request, pack_id)
    if adapter_registry is not None and release is not None:
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


@router.get("/devices/{connection_target}/telemetry")
async def pack_device_telemetry_route(
    request: Request,
    connection_target: str,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    device_type: str = Query(...),
    connection_type: str | None = Query(None),
    ip_address: str | None = Query(None),
) -> dict[str, Any]:
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    release = _release_for_pack(request, pack_id)
    telemetry = (
        await adapter_telemetry(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            identity_value=connection_target,
            connection_target=connection_target,
        )
        if adapter_registry is not None and release is not None
        else None
    )
    if telemetry is None:
        raise HTTPException(status_code=404, detail=f"Device {connection_target} not found or not connected")
    return telemetry


@router.post("/devices/{connection_target}/lifecycle/{action}")
async def pack_device_lifecycle_route(
    request: Request,
    connection_target: str,
    action: str,
    pack_id: str = Query(...),
    platform_id: str = Query(...),
    args: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    platform_def = resolve_desired_platform(_latest_desired(request), pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown desired pack platform {pack_id}:{platform_id}")
    adapter_registry = getattr(request.app.state, "adapter_registry", None)
    host_identity = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""
    release = _release_for_pack(request, pack_id)
    if adapter_registry is not None and release is not None:
        payload = await adapter_lifecycle_action(
            adapter_registry=adapter_registry,
            pack_id=pack_id,
            pack_release=release,
            host_id=host_id_value or "",
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


@router.post("/features/{feature_id}/actions/{action_id}")
async def feature_action_route(
    request: Request,
    feature_id: str,
    action_id: str,
    body: FeatureActionRequest,
) -> dict[str, Any]:
    adapter_registry: AdapterRegistry | None = getattr(request.app.state, "adapter_registry", None)
    adapter = adapter_registry.get_current(body.pack_id) if adapter_registry is not None else None
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {body.pack_id!r}")

    host_identity: HostIdentity | None = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""

    ctx = _FeatureActionContext(
        host_id=host_id_value or "",
        device_identity_value=body.device_identity_value or "",
    )
    result = await dispatch_feature_action(adapter, feature_id, action_id, body.args, ctx)
    return {"ok": result.ok, "detail": result.detail, "data": result.data}


@router.post("/devices/normalize", response_model=NormalizeDeviceResponse)
async def normalize_device_route(request: Request, req: NormalizeDeviceRequest) -> dict[str, Any]:
    adapter_registry: AdapterRegistry | None = getattr(request.app.state, "adapter_registry", None)
    host_identity: HostIdentity | None = getattr(request.app.state, "host_identity", None)
    host_id_value = host_identity.get() if host_identity is not None else ""
    if adapter_registry is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")

    result = await adapter_normalize_device(
        adapter_registry=adapter_registry,
        pack_id=req.pack_id,
        pack_release=req.pack_release,
        host_id=host_id_value or "",
        platform_id=req.platform_id,
        raw_input=req.raw_input,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"No adapter loaded for pack {req.pack_id!r}")
    return result
