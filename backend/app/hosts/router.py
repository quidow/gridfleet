from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from app.agent_comm import operations as agent_operations
from app.agent_comm.dependencies import AgentCommServicesDep
from app.core.database import async_session
from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.http_errors import found_or_404
from app.core.timeutil import now_utc
from app.devices.dependencies import DeviceServicesDep
from app.devices.services import platform_label as platform_label_service
from app.devices.services.identity_conflicts import DeviceIdentityConflictError
from app.events.dependencies import EventServicesDep
from app.hosts import service as host_service
from app.hosts import service_versioning as host_versioning
from app.hosts.dependencies import HostServicesDep
from app.hosts.liveness import effective_host_status, host_online
from app.hosts.models import Host
from app.hosts.schemas import (
    DiscoveryConfirm,
    DiscoveryConfirmResult,
    DiscoveryResult,
    HostCreate,
    HostDetail,
    HostDiagnosticsRead,
    HostEventsPage,
    HostRead,
    HostRegister,
    HostResourceTelemetryResponse,
    HostToolEnvRead,
    HostToolEnvUpdate,
    HostToolStatusRead,
    IntakeCandidateRead,
)
from app.packs import schemas as pack_schemas
from app.packs.dependencies import PackServicesDep
from app.settings.dependencies import SettingsServicesDep

if TYPE_CHECKING:
    from app.core.type_defs import AsyncTaskFactory
    from app.events.protocols import EventPublisher
    from app.events.services_container import EventServices
    from app.hosts.service import HostCrudService
    from app.hosts.services_container import HostServices
    from app.packs.protocols import PackDiscoveryProtocol
    from app.packs.services_container import PackServices

HOST_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/hosts", tags=["hosts"], responses=HOST_ERROR_RESPONSES)
logger = logging.getLogger(__name__)
get_agent_tool_status = agent_operations.get_tool_status

_background_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(task_fn: AsyncTaskFactory, *args: object, **kwargs: object) -> None:
    """Schedule a coroutine factory as a background task with proper reference tracking."""
    task = asyncio.create_task(task_fn(*args, **kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _schedule_host_acceptance_tasks(
    host_id: uuid.UUID,
    *,
    event_services: EventServices,
    pack_services: PackServices,
    host_services: HostServices,
) -> None:
    _fire_and_forget(
        _auto_discover,
        host_id,
        event_services.publisher,
        pack_services.discovery,
        host_services.crud,
    )


def _serialize_host(host: Host, settings_services: SettingsServicesDep) -> dict[str, Any]:
    min_version = settings_services.service.get("agent.min_version")
    required_version = host_versioning.normalize_agent_version_setting(min_version)
    rec_version = settings_services.service.get("agent.recommended_version")
    recommended_version = host_versioning.normalize_agent_version_setting(rec_version)
    payload = HostRead.model_validate(host).model_dump()
    offline_after = settings_services.service.get_float("general.host_offline_after_sec")
    payload["status"] = effective_host_status(host, offline_after_sec=offline_after)
    payload["required_agent_version"] = required_version
    payload["recommended_agent_version"] = recommended_version
    payload["agent_version_status"] = host_versioning.get_agent_version_status(host.agent_version, required_version)
    payload["agent_update_available"] = host_versioning.is_agent_update_available(
        host.agent_version,
        recommended_version,
    )
    payload["capabilities"] = host_service.normalize_capabilities(payload.get("capabilities"))
    return payload


async def _auto_discover(
    host_id: uuid.UUID,
    publisher: EventPublisher,
    discovery: PackDiscoveryProtocol,
    crud: HostCrudService,
) -> None:
    """Background task: trigger device discovery for a newly accepted host."""
    try:
        async with async_session() as db:
            host = await crud.get_host(db, host_id)
            if host is None:
                return
            result = await discovery.discover_devices(db, host)
            if result.new_devices:
                await publisher.publish(
                    "host.discovery_completed",
                    {
                        "host_id": str(host_id),
                        "hostname": host.hostname,
                        "new_device_count": len(result.new_devices),
                    },
                )
    except Exception:
        logger.exception("Auto-discovery failed for host %s", host_id)


@router.post("/register", response_model=HostRead)
async def register_host(
    data: HostRegister,
    response: Response,
    db: DbDep,
    host_services: HostServicesDep,
    event_services: EventServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    pack_services: PackServicesDep,
) -> dict[str, Any]:
    try:
        host, is_new = await host_services.crud.register_host(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Host registration conflict") from None
    except ValueError as exc:
        raise HTTPException(status_code=426, detail=str(exc)) from None

    if not is_new:
        # A re-registering agent is live evidence the backend can reach it again. If its
        # circuit breaker is open (the agent was unreachable, e.g. mid-restart), close it now
        # so the reconciler re-observes the node on the next tick instead of waiting out the
        # cooldown — otherwise the device can be reported recovered while its AppiumNode row
        # still holds the stale pre-restart pid (the S27 agent-restart no-op race). It is a
        # no-op when the breaker is already closed, so healthy periodic refreshes are unaffected.
        await agent_comm.circuit_breaker.record_success(host.ip)

    if is_new:
        response.status_code = 201
        if settings_services.service.get("agent.auto_accept_hosts"):
            _schedule_host_acceptance_tasks(
                host.id,
                event_services=event_services,
                pack_services=pack_services,
                host_services=host_services,
            )

    return _serialize_host(host, settings_services)


@router.post("/{host_id}/approve", response_model=HostRead)
async def approve_host(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    event_services: EventServicesDep,
    settings_services: SettingsServicesDep,
    pack_services: PackServicesDep,
) -> dict[str, Any]:
    host = found_or_404(await host_services.crud.approve_host(db, host_id), "Host not found or not pending")
    _schedule_host_acceptance_tasks(
        host.id,
        event_services=event_services,
        pack_services=pack_services,
        host_services=host_services,
    )
    return _serialize_host(host, settings_services)


@router.post("/{host_id}/reject", status_code=204)
async def reject_host(host_id: uuid.UUID, db: DbDep, host_services: HostServicesDep) -> None:
    rejected = await host_services.crud.reject_host(db, host_id)
    if not rejected:
        raise HTTPException(status_code=404, detail="Host not found or not pending")


@router.post("", response_model=HostRead, status_code=201)
async def create_host(
    data: HostCreate, db: DbDep, host_services: HostServicesDep, settings_services: SettingsServicesDep
) -> dict[str, Any]:
    try:
        host = await host_services.crud.create_host(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Host with this hostname already exists") from None
    return _serialize_host(host, settings_services)


@router.get("", response_model=list[HostRead])
async def list_hosts(
    db: DbDep, host_services: HostServicesDep, settings_services: SettingsServicesDep
) -> list[dict[str, Any]]:
    return [_serialize_host(host, settings_services) for host in await host_services.crud.list_hosts(db)]


@router.get("/{host_id}", response_model=HostDetail)
async def get_host(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
) -> dict[str, Any]:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")

    payload = _serialize_host(host, settings_services)
    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in host.devices),
    )
    payload["devices"] = [
        await device_services.presenter.serialize_device(
            db,
            device,
            platform_label=label_map.get((device.pack_id, device.platform_id)),
        )
        for device in host.devices
    ]
    return payload


@router.get(
    "/{host_id}/events",
    response_model=HostEventsPage,
    summary="Persisted backend events scoped to a host",
)
async def get_host_events(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    types: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HostEventsPage:
    type_list = [token.strip() for token in types.split(",") if token.strip()] if types else None
    return await host_services.host_events.query_host_events(
        db,
        host_id=host_id,
        types=type_list,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get("/{host_id}/driver-packs", response_model=pack_schemas.HostDriverPacksOut)
async def host_driver_packs(
    host_id: uuid.UUID, db: DbDep, pack_services: PackServicesDep
) -> pack_schemas.HostDriverPacksOut:
    found_or_404(await db.get(Host, host_id), "host not found")
    return pack_schemas.HostDriverPacksOut.model_validate(
        await pack_services.status.get_host_driver_pack_status(db, host_id)
    )


@router.post(
    "/{host_id}/driver-packs/{pack_id}/doctor",
    response_model=list[pack_schemas.HostPackDoctorOut],
)
async def trigger_driver_doctor(
    host_id: uuid.UUID,
    pack_id: str,
    db: DbDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    pack_services: PackServicesDep,
) -> list[pack_schemas.HostPackDoctorOut]:
    host = found_or_404(await db.get(Host, host_id), "host not found")
    offline_after = settings_services.service.get_float("general.host_offline_after_sec")
    if not host_online(host, offline_after_sec=offline_after):
        raise HTTPException(status_code=409, detail="host must be online to run doctor checks")

    checks = await agent_operations.pack_doctor(
        host.ip,
        host.agent_port,
        pack_id,
        settings=settings_services.service,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )

    await pack_services.status.persist_doctor_results(db, host_id, pack_id, checks)
    await db.commit()

    return [
        pack_schemas.HostPackDoctorOut(
            pack_id=pack_id,
            check_id=c["check_id"],
            ok=c["ok"],
            message=c.get("message", ""),
        )
        for c in checks
    ]


@router.get("/{host_id}/diagnostics", response_model=HostDiagnosticsRead)
async def get_host_diagnostics(host_id: uuid.UUID, db: DbDep, host_services: HostServicesDep) -> HostDiagnosticsRead:
    return found_or_404(await host_services.diagnostics.get_host_diagnostics(db, host_id), "Host not found")


@router.get("/{host_id}/resource-telemetry", response_model=HostResourceTelemetryResponse)
async def get_host_resource_telemetry(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    settings_services: SettingsServicesDep,
    since: datetime | None = None,
    until: datetime | None = None,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 5,
) -> HostResourceTelemetryResponse:
    window_end = until or now_utc()
    default_window_minutes = int(settings_services.service.get("general.host_resource_telemetry_window_minutes"))
    window_start = since or (window_end - timedelta(minutes=default_window_minutes))
    try:
        payload = await host_services.resource_telemetry.fetch_host_resource_telemetry(
            db,
            host_id,
            since=window_start,
            until=window_end,
            bucket_minutes=bucket_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = found_or_404(payload, "Host not found")
    return payload


@router.get("/{host_id}/tools/status", response_model=HostToolStatusRead)
async def get_host_tool_status(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
) -> dict[str, Any]:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    offline_after = settings_services.service.get_float("general.host_offline_after_sec")
    if not host_online(host, offline_after_sec=offline_after):
        raise HTTPException(status_code=400, detail="Host must be online to fetch tool status")
    return await get_agent_tool_status(
        host.ip,
        host.agent_port,
        settings=settings_services.service,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )


@router.delete("/{host_id}", status_code=204)
async def delete_host(host_id: uuid.UUID, db: DbDep, host_services: HostServicesDep) -> None:
    try:
        deleted = await host_services.crud.delete_host(db, host_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="Host not found")


@router.post("/{host_id}/discover", response_model=DiscoveryResult)
async def discover_devices(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> DiscoveryResult:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    return await pack_services.discovery.discover_devices(db, host)


@router.get("/{host_id}/intake-candidates", response_model=list[IntakeCandidateRead])
async def intake_candidates(
    host_id: uuid.UUID,
    db: DbDep,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> list[IntakeCandidateRead]:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    return await pack_services.discovery.list_intake_candidates(db, host)


@router.post("/{host_id}/discover/confirm", response_model=DiscoveryConfirmResult)
async def confirm_discovery(
    host_id: uuid.UUID,
    data: DiscoveryConfirm,
    db: DbDep,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> DiscoveryConfirmResult:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    # Re-run discovery to get fresh data for validation
    result = await pack_services.discovery.discover_devices(db, host)
    try:
        return await pack_services.discovery.confirm_discovery(
            db,
            host,
            data.add_identity_values,
            data.remove_identity_values,
            result,
        )
    except DeviceIdentityConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{host_id}/tool-env",
    response_model=HostToolEnvRead,
    status_code=200,
    summary="Get per-host tool environment variables",
)
async def get_host_tool_env(host_id: uuid.UUID, db: DbDep, host_services: HostServicesDep) -> dict[str, Any]:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    return {"env": host.tool_env or {}}


@router.put(
    "/{host_id}/tool-env",
    response_model=HostToolEnvRead,
    status_code=200,
    summary="Set per-host tool environment variables",
)
async def put_host_tool_env(
    host_id: uuid.UUID,
    body: HostToolEnvUpdate,
    db: DbDep,
    host_services: HostServicesDep,
) -> dict[str, Any]:
    host = found_or_404(await host_services.crud.get_host(db, host_id), "Host not found")
    host.tool_env = body.env if body.env else None
    await db.commit()
    return {"env": host.tool_env or {}}
