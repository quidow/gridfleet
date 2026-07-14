"""HTTP routes for ``/agent/pack/*``."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Query, Request, status

from agent_app.error_codes import AgentErrorCode, ErrorEnvelope, http_exc
from agent_app.pack.adapter_dispatch import (
    adapter_supports,
    dispatch_doctor,
    dispatch_health_check,
    dispatch_lifecycle_action,
    dispatch_normalize_device,
    dispatch_telemetry,
)
from agent_app.pack.constants import PACK_ID_PATTERN, PLATFORM_ID_PATTERN
from agent_app.pack.contexts import DoctorCtx, HealthCtx, LifecycleCtx, NormalizeCtx, TelemetryCtx
from agent_app.pack.dependencies import (
    DesiredPlatformDep,
    HostIdDep,
    OptionalAdapterRegistryDep,
    PackStateLoopDep,
)
from agent_app.pack.discovery import enumerate_pack_candidates
from agent_app.pack.schemas import (
    NormalizeDeviceRequest,
    NormalizeDeviceResponse,
    PackDeviceHealthResponse,
    PackDeviceLifecycleResponse,
    PackDevicesResponse,
    PackDoctorResponse,
)

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.adapter_types import HardwareTelemetry, HealthCheckResult, LifecycleActionResult
    from agent_app.pack.manifest import DesiredPlatform
    from agent_app.pack.worker_supervisor import WorkerHandle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/pack", tags=["pack"])

PackIdQuery = Annotated[str, Query(min_length=1, pattern=PACK_ID_PATTERN)]
PlatformIdQuery = Annotated[str, Query(min_length=1, pattern=PLATFORM_ID_PATTERN)]


def worker_or_none(registry: AdapterRegistry | None, pack_id: str, release: str) -> WorkerHandle | None:
    return registry.get(pack_id, release) if registry is not None else None


def _adapter_health_payload(results: list[HealthCheckResult]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "healthy": all(result.ok for result in results),
        "checks": [
            {"check_id": result.check_id, "ok": result.ok, "message": result.detail, "debounce": result.debounce}
            for result in results
        ],
    }
    for result in results:
        if result.recommended_action:
            payload["recommended_action"] = result.recommended_action
            break
    return payload


def _adapter_lifecycle_payload(result: LifecycleActionResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": result.ok, "state": result.state, "detail": result.detail}
    if result.resolved_connection_target is not None:
        payload["resolved_connection_target"] = result.resolved_connection_target
    return payload


async def run_device_health_probe(
    *,
    adapter_registry: AdapterRegistry | None,
    platform: DesiredPlatform,
    release: str,
    pack_id: str,
    platform_id: str,
    connection_target: str,
    device_type: str,
    connection_type: str | None = None,
    ip_address: str | None = None,
    allow_boot: bool = False,
    headless: bool | None = None,
    ip_ping_timeout_sec: float | None = None,
    ip_ping_count: int | None = None,
    identity_value: str | None = None,
    claimed_ports: dict[str, int] | None = None,
    has_live_session: bool | None = None,
) -> dict[str, Any]:
    """Run the pack health hook with the same context used by the HTTP route."""
    del platform, headless
    handle = worker_or_none(adapter_registry, pack_id, release)
    if handle is not None and adapter_supports(handle, "health_check"):
        results = await dispatch_health_check(
            handle,
            HealthCtx(
                device_identity_value=connection_target,
                allow_boot=allow_boot,
                platform_id=platform_id,
                device_type=device_type,
                connection_type=connection_type,
                ip_address=ip_address,
                ip_ping_timeout_sec=ip_ping_timeout_sec,
                ip_ping_count=ip_ping_count,
                expected_identity_value=identity_value,
                claimed_ports=claimed_ports,
                has_live_session=has_live_session,
            ),
        )
        return _adapter_health_payload(results)
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


async def run_device_lifecycle_state_probe(
    *,
    adapter_registry: AdapterRegistry | None,
    platform: DesiredPlatform,
    release: str,
    pack_id: str,
    platform_id: str,
    connection_target: str,
    host_id: str,
    identity_value: str | None = None,
) -> dict[str, Any] | None:
    """Run the pack ``state`` lifecycle action with the same dispatch as the HTTP
    route. Returns None when no adapter/action is available (caller reports error)."""
    del platform, platform_id, identity_value
    handle = worker_or_none(adapter_registry, pack_id, release)
    if handle is None or not adapter_supports(handle, "lifecycle_action"):
        return None
    result = await dispatch_lifecycle_action(
        handle,
        "state",
        {},
        LifecycleCtx(host_id=host_id, device_identity_value=connection_target),
    )
    return _adapter_lifecycle_payload(result)


async def run_device_telemetry_probe(
    *,
    adapter_registry: AdapterRegistry | None,
    pack_id: str,
    release: str,
    identity_value: str,
    connection_target: str,
) -> dict[str, Any] | None:
    """Run the pack telemetry hook with the same dispatch as the HTTP route."""
    if adapter_registry is None:
        return None
    handle = worker_or_none(adapter_registry, pack_id, release)
    if handle is None or not adapter_supports(handle, "telemetry"):
        return None
    result: HardwareTelemetry = await dispatch_telemetry(
        handle,
        TelemetryCtx(device_identity_value=identity_value, connection_target=connection_target),
    )
    if not result.supported:
        return {"support_status": "unsupported"}
    return {
        "support_status": "supported",
        "battery_level_percent": result.battery_level_percent,
        "battery_temperature_c": result.battery_temperature_c,
        "charging_state": result.charging_state,
    }


def _parse_claimed_ports(raw: str | None) -> dict[str, int] | None:
    """Lenient decode of the claimed-ports JSON query param; malformed input
    degrades to None (adapter skips port checks) rather than failing the probe."""
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            return None
        return {str(k): int(v) for k, v in decoded.items()}
    except ValueError, TypeError:
        return None


@router.get(
    "/devices",
    response_model=PackDevicesResponse,
    status_code=status.HTTP_200_OK,
    summary="Pack-aware enumeration of candidate devices",
)
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
    "/devices/{connection_target}/health",
    response_model=PackDeviceHealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Pack-shaped device health check via adapter",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "UNKNOWN_PLATFORM"},
    },
)
async def pack_device_health_route(
    connection_target: str,
    platform: DesiredPlatformDep,
    adapter_registry: OptionalAdapterRegistryDep,
    pack_id: PackIdQuery,
    platform_id: PlatformIdQuery,
    device_type: Annotated[str, Query(min_length=1)],
    connection_type: Annotated[str | None, Query()] = None,
    ip_address: Annotated[str | None, Query()] = None,
    allow_boot: Annotated[bool, Query()] = False,
    headless: Annotated[bool, Query()] = True,
    ip_ping_timeout_sec: Annotated[float | None, Query(gt=0)] = None,
    ip_ping_count: Annotated[int | None, Query(ge=1)] = None,
    identity_value: Annotated[str | None, Query()] = None,
    claimed_ports: Annotated[str | None, Query()] = None,
    has_live_session: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    _platform_def, release = platform
    return await run_device_health_probe(
        adapter_registry=adapter_registry,
        platform=_platform_def,
        release=release,
        pack_id=pack_id,
        platform_id=platform_id,
        connection_target=connection_target,
        device_type=device_type,
        connection_type=connection_type,
        ip_address=ip_address,
        allow_boot=allow_boot,
        headless=headless,
        ip_ping_timeout_sec=ip_ping_timeout_sec,
        ip_ping_count=ip_ping_count,
        identity_value=identity_value,
        claimed_ports=_parse_claimed_ports(claimed_ports),
        has_live_session=has_live_session,
    )


@router.post(
    "/devices/{connection_target}/lifecycle/{action}",
    response_model=PackDeviceLifecycleResponse,
    status_code=status.HTTP_200_OK,
    summary="Dispatch a lifecycle action through the adapter",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "UNKNOWN_PLATFORM"},
    },
)
async def pack_device_lifecycle_route(
    request: Request,
    connection_target: str,
    action: str,
    platform: DesiredPlatformDep,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
    pack_id: PackIdQuery,
    platform_id: PlatformIdQuery,
    args: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> dict[str, Any]:
    _platform_def, release = platform
    handle = worker_or_none(adapter_registry, pack_id, release)
    if handle is not None and adapter_supports(handle, "lifecycle_action"):
        result = await dispatch_lifecycle_action(
            handle,
            action,
            args,
            LifecycleCtx(host_id=host_id, device_identity_value=connection_target),
        )
        # A successful state-changing action (e.g. the backend's reconnect /
        # release_forwarded_ports link repair) should be re-observed promptly
        # rather than at the next fixed probe cadence. "state" is a read-only
        # poll, so it never wakes the loop.
        if result.ok and action != "state":
            probe_loop = getattr(request.app.state, "probe_loop", None)
            if probe_loop is not None:
                probe_loop.request_immediate("device_health")
        return _adapter_lifecycle_payload(result)
    return {
        "success": False,
        "detail": f"Adapter not loaded for pack {pack_id}:{platform_id}",
    }


@router.post(
    "/devices/normalize",
    response_model=NormalizeDeviceResponse,
    status_code=status.HTTP_200_OK,
    summary="Normalize raw device input into pack canonical form",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope, "description": "NO_ADAPTER"},
    },
)
async def normalize_device_route(
    req: NormalizeDeviceRequest,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
) -> dict[str, Any]:
    if adapter_registry is None:
        raise http_exc(
            status_code=404,
            code=AgentErrorCode.NO_ADAPTER,
            message=f"No adapter loaded for pack {req.pack_id!r}",
        )

    handle = worker_or_none(adapter_registry, req.pack_id, req.pack_release)
    if handle is None or not adapter_supports(handle, "normalize_device"):
        raise http_exc(
            status_code=404,
            code=AgentErrorCode.NO_ADAPTER,
            message=f"No adapter loaded for pack {req.pack_id!r}",
        )
    result = await dispatch_normalize_device(
        handle,
        NormalizeCtx(host_id=host_id, platform_id=req.platform_id, raw_input=req.raw_input),
    )
    return dataclasses.asdict(result)


@router.post(
    "/{pack_id}/doctor",
    response_model=PackDoctorResponse,
    status_code=status.HTTP_200_OK,
    summary="Run doctor checks for a specific driver pack",
)
async def pack_doctor_route(
    pack_id: str,
    adapter_registry: OptionalAdapterRegistryDep,
    host_id: HostIdDep,
) -> dict[str, Any]:
    adapter = adapter_registry.get_current(pack_id) if adapter_registry is not None else None
    if adapter is None or not adapter_supports(adapter, "doctor"):
        return {"checks": []}
    try:
        results = await dispatch_doctor(adapter, DoctorCtx(host_id=host_id))
    except Exception as exc:
        safe_id = pack_id.replace("\n", "").replace("\r", "")[:64]
        logger.exception("adapter doctor failed for pack %s", safe_id)
        msg = f"adapter doctor failed: {type(exc).__name__}"
        return {"checks": [{"check_id": "adapter_doctor", "ok": False, "message": msg}]}
    return {
        "checks": [{"check_id": r.check_id, "ok": r.ok, "message": r.message} for r in results],
    }
