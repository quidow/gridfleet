import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from app.agent_comm import operations as agent_operations
from app.database import async_session
from app.dependencies import DbDep
from app.events import event_bus
from app.hosts import service as host_service
from app.hosts import service_diagnostics as host_diagnostics
from app.hosts import service_resource_telemetry as host_resource_telemetry
from app.hosts import service_versioning as host_versioning
from app.hosts.models import Host
from app.hosts.schemas import (
    DiscoveryConfirm,
    DiscoveryConfirmResult,
    DiscoveryResult,
    HostCreate,
    HostDetail,
    HostDiagnosticsRead,
    HostRead,
    HostRegister,
    HostResourceTelemetryResponse,
    HostToolStatusRead,
    IntakeCandidateRead,
)
from app.packs import schemas as pack_schemas
from app.packs.services import status as pack_status
from app.plugins import service as plugin_service
from app.services import (
    device_presenter,
    pack_discovery_service,
    platform_label_service,
)
from app.services.device_identity_conflicts import DeviceIdentityConflictError
from app.settings import settings_service
from app.type_defs import AsyncTaskFactory

router = APIRouter(prefix="/api/hosts", tags=["hosts"])
logger = logging.getLogger(__name__)
get_agent_tool_status = agent_operations.get_tool_status
get_pack_devices = agent_operations.get_pack_devices

_background_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(task_fn: AsyncTaskFactory, *args: object) -> None:
    """Schedule a coroutine factory as a background task with proper reference tracking."""
    task = asyncio.create_task(task_fn(*args))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _serialize_host(host: Host) -> dict[str, Any]:
    required_version = host_versioning.normalize_agent_version_setting(settings_service.get("agent.min_version"))
    recommended_version = host_versioning.normalize_agent_version_setting(
        settings_service.get("agent.recommended_version")
    )
    payload = HostRead.model_validate(host).model_dump()
    payload["required_agent_version"] = required_version
    payload["recommended_agent_version"] = recommended_version
    payload["agent_version_status"] = host_versioning.get_agent_version_status(host.agent_version, required_version)
    payload["agent_update_available"] = host_versioning.is_agent_update_available(
        host.agent_version,
        recommended_version,
    )
    payload["capabilities"] = host_service.normalize_capabilities(payload.get("capabilities"))
    return payload


async def _auto_discover(host_id: uuid.UUID) -> None:
    """Background task: trigger device discovery for a newly accepted host."""
    try:
        async with async_session() as db:
            host = await host_service.get_host(db, host_id)
            if host is None:
                return
            result = await pack_discovery_service.discover_devices(db, host, agent_get_pack_devices=get_pack_devices)
            if result.new_devices:
                await event_bus.publish(
                    "host.discovery_completed",
                    {
                        "host_id": str(host_id),
                        "hostname": host.hostname,
                        "new_device_count": len(result.new_devices),
                    },
                )
    except Exception:
        logger.exception("Auto-discovery failed for host %s", host_id)


async def _auto_prepare_host_diagnostics(host_id: uuid.UUID) -> None:
    try:
        async with async_session() as db:
            host = await host_service.get_host(db, host_id)
            if host is None:
                return
            plugins = await plugin_service.list_plugins(db)
            await plugin_service.auto_sync_host_plugins(host, plugins)
    except Exception:
        logger.exception("Automatic diagnostics preparation failed for host %s", host_id)


@router.post("/register", response_model=HostRead)
async def register_host(data: HostRegister, response: Response, db: DbDep) -> dict[str, Any]:
    try:
        host, is_new = await host_service.register_host(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Host registration conflict") from None

    if is_new:
        response.status_code = 201
        if settings_service.get("agent.auto_accept_hosts"):
            _fire_and_forget(_auto_discover, host.id)
            _fire_and_forget(_auto_prepare_host_diagnostics, host.id)

    return _serialize_host(host)


@router.post("/{host_id}/approve", response_model=HostRead)
async def approve_host(host_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    host = await host_service.approve_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found or not pending")
    _fire_and_forget(_auto_discover, host.id)
    _fire_and_forget(_auto_prepare_host_diagnostics, host.id)
    return _serialize_host(host)


@router.post("/{host_id}/reject", status_code=204)
async def reject_host(host_id: uuid.UUID, db: DbDep) -> None:
    rejected = await host_service.reject_host(db, host_id)
    if not rejected:
        raise HTTPException(status_code=404, detail="Host not found or not pending")


@router.post("", response_model=HostRead, status_code=201)
async def create_host(data: HostCreate, db: DbDep) -> dict[str, Any]:
    try:
        host = await host_service.create_host(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Host with this hostname already exists") from None
    return _serialize_host(host)


@router.get("", response_model=list[HostRead])
async def list_hosts(db: DbDep) -> list[dict[str, Any]]:
    return [_serialize_host(host) for host in await host_service.list_hosts(db)]


@router.get("/capabilities")
async def host_capabilities() -> dict[str, bool]:
    return {"web_terminal_enabled": bool(settings_service.get("agent.enable_web_terminal"))}


@router.get("/{host_id}", response_model=HostDetail)
async def get_host(host_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")

    payload = _serialize_host(host)
    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in host.devices),
    )
    payload["devices"] = [
        await device_presenter.serialize_device(
            db,
            device,
            platform_label=label_map.get((device.pack_id, device.platform_id)),
        )
        for device in host.devices
    ]
    return payload


@router.get("/{host_id}/driver-packs", response_model=pack_schemas.HostDriverPacksOut)
async def host_driver_packs(host_id: uuid.UUID, db: DbDep) -> pack_schemas.HostDriverPacksOut:
    host = await db.get(Host, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    return pack_schemas.HostDriverPacksOut.model_validate(await pack_status.get_host_driver_pack_status(db, host_id))


@router.get("/{host_id}/diagnostics", response_model=HostDiagnosticsRead)
async def get_host_diagnostics(host_id: uuid.UUID, db: DbDep) -> HostDiagnosticsRead:
    payload = await host_diagnostics.get_host_diagnostics(db, host_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return payload


@router.get("/{host_id}/resource-telemetry", response_model=HostResourceTelemetryResponse)
async def get_host_resource_telemetry(
    host_id: uuid.UUID,
    db: DbDep,
    since: datetime | None = None,
    until: datetime | None = None,
    bucket_minutes: int = Query(5, ge=1, le=1440),
) -> HostResourceTelemetryResponse:
    window_end = until or datetime.now(UTC)
    default_window_minutes = int(settings_service.get("general.host_resource_telemetry_window_minutes"))
    window_start = since or (window_end - timedelta(minutes=default_window_minutes))
    try:
        payload = await host_resource_telemetry.fetch_host_resource_telemetry(
            db,
            host_id,
            since=window_start,
            until=window_end,
            bucket_minutes=bucket_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return payload


@router.get("/{host_id}/tools/status", response_model=HostToolStatusRead, response_model_exclude_none=True)
async def get_host_tool_status(host_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.status.value != "online":
        raise HTTPException(status_code=400, detail="Host must be online to fetch tool status")
    return await get_agent_tool_status(
        host.ip,
        host.agent_port,
    )


@router.delete("/{host_id}", status_code=204)
async def delete_host(host_id: uuid.UUID, db: DbDep) -> None:
    try:
        deleted = await host_service.delete_host(db, host_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="Host not found")


@router.post("/{host_id}/discover", response_model=DiscoveryResult)
async def discover_devices(host_id: uuid.UUID, db: DbDep) -> DiscoveryResult:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return await pack_discovery_service.discover_devices(db, host, agent_get_pack_devices=get_pack_devices)


@router.get("/{host_id}/intake-candidates", response_model=list[IntakeCandidateRead])
async def intake_candidates(host_id: uuid.UUID, db: DbDep) -> list[IntakeCandidateRead]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return await pack_discovery_service.list_intake_candidates(db, host, agent_get_pack_devices=get_pack_devices)


@router.post("/{host_id}/discover/confirm", response_model=DiscoveryConfirmResult)
async def confirm_discovery(host_id: uuid.UUID, data: DiscoveryConfirm, db: DbDep) -> DiscoveryConfirmResult:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    # Re-run discovery to get fresh data for validation
    result = await pack_discovery_service.discover_devices(db, host, agent_get_pack_devices=get_pack_devices)
    try:
        return await pack_discovery_service.confirm_discovery(
            db,
            host,
            data.add_identity_values,
            data.remove_identity_values,
            result,
        )
    except DeviceIdentityConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
