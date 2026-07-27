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
from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.http_errors import convert_missing_row, found_or_404
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
from app.hosts.service import HostTarget
from app.packs import schemas as pack_schemas
from app.packs.dependencies import PackServicesDep
from app.packs.services.discovery import StaleHostGenerationError
from app.settings.dependencies import SettingsServicesDep

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.core.type_defs import AsyncTaskFactory, SessionFactory
    from app.events.protocols import EventPublisher
    from app.events.services_container import EventServices
    from app.hosts.service import HostCommandSnapshot, HostCrudService
    from app.hosts.services_container import HostServices
    from app.packs.protocols import PackDiscoveryProtocol
    from app.packs.services_container import PackServices
    from app.settings.services_container import SettingsServices

HOST_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES
# Default Host Detail telemetry window when the client sends no since/until.
HOST_TELEMETRY_WINDOW_MINUTES = 60

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
        host_services.session_factory,
    )


def _serialize_host(host: Host | HostCommandSnapshot, settings_services: SettingsServicesDep) -> dict[str, Any]:
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


async def _load_host_target(host_services: HostServices, host_id: uuid.UUID) -> HostTarget:
    """Copy the scalars an agent call needs, then let the read session close.

    Everything downstream of this — the dial and any write transaction that
    follows it — works from the returned value, so no session and no row lock
    survives into the network call.
    """
    async with host_services.session_factory() as db:
        target = await host_services.crud.load_host_target(db, host_id)
    return found_or_404(target, "Host not found")


async def _online_host_target(
    host_services: HostServices,
    settings_services: SettingsServices,
    host_id: uuid.UUID,
    *,
    missing_detail: str,
    offline_status: int,
    offline_detail: str,
) -> HostTarget:
    """The liveness-gated variant, for routes that refuse to dial an offline host.

    Both details are parameters because the two callers disagree on each of
    them: the doctor route's missing-host body is lowercase and its offline
    status is 409; tool status uses sentence case and 400. Those are the bodies
    their clients already see, so neither is normalised here.
    """
    offline_after = settings_services.service.get_float("general.host_offline_after_sec")
    online = False
    async with host_services.session_factory() as db:
        host = await db.get(Host, host_id)
        target = None if host is None else HostTarget.from_host(host)
        if host is not None:
            online = host_online(host, offline_after_sec=offline_after)
    resolved = found_or_404(target, missing_detail)
    if not online:
        raise HTTPException(status_code=offline_status, detail=offline_detail)
    return resolved


async def _auto_discover(
    host_id: uuid.UUID,
    publisher: EventPublisher,
    discovery: PackDiscoveryProtocol,
    crud: HostCrudService,
    session_factory: SessionFactory,
) -> None:
    """Background task: trigger device discovery for a newly accepted host.

    Takes the host container's factory rather than the module-global session so
    the same prepare/dial/classify split — and the tests that observe it — apply
    to the one discovery caller that runs outside a request.
    """
    try:
        async with session_factory() as db:
            target = await crud.load_host_target(db, host_id)
        if target is None:
            return
        candidates = await discovery.fetch_pack_candidates(target)
        async with session_factory() as db:
            result = await discovery.classify_discovery(db, target.host_id, candidates)
        if result.new_devices:
            # Standalone summary: the classification is read-only and every
            # source effect has already committed.
            await publisher.publish(
                "host.discovery_completed",
                {
                    "host_id": str(host_id),
                    "hostname": target.hostname,
                    "new_device_count": len(result.new_devices),
                },
            )
    except Exception:
        logger.exception("Auto-discovery failed for host %s", host_id)


async def _register_host_txn(host_services: HostServices, data: HostRegister) -> tuple[HostCommandSnapshot, bool]:
    """One registration attempt inside its own transaction."""
    async with host_services.session_factory.begin() as db:
        return await host_services.crud.register_host(db, data)


@router.post("/register", response_model=HostRead)
async def register_host(
    data: HostRegister,
    response: Response,
    host_services: HostServicesDep,
    event_services: EventServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    pack_services: PackServicesDep,
) -> dict[str, Any]:
    try:
        # Hoisted ahead of the attempt so the orchestration-contract gate visibly
        # precedes *both* transactions: the fallback below reapplies the same
        # payload and does not re-validate it.
        host_service.validate_orchestration_contract(data.capabilities, host_label=data.hostname)
        host, is_new = await _register_host_txn(host_services, data)
    except ValueError as exc:
        raise HTTPException(status_code=426, detail=str(exc)) from None
    except IntegrityError as exc:
        if not host_service.is_hostname_conflict(exc):
            raise HTTPException(status_code=409, detail="Host registration conflict") from None
        # A concurrent peer (e.g. a heartbeat-driven re-register racing the
        # operator-initiated registration) committed the same hostname between
        # our locked SELECT and our INSERT. The attempt's transaction is already
        # rolled back and its session closed by the context above; the fallback
        # opens a fresh one and retakes the Host lock from scratch.
        async with host_services.session_factory.begin() as db:
            existing = await host_services.crud.reregister_host(db, data)
        if existing is None:
            raise HTTPException(status_code=409, detail="Host registration conflict") from None
        host, is_new = existing, False

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
    host_services: HostServicesDep,
    event_services: EventServicesDep,
    settings_services: SettingsServicesDep,
    pack_services: PackServicesDep,
) -> dict[str, Any]:
    async with host_services.session_factory.begin() as db:
        approved = await host_services.crud.approve_host(db, host_id)
    host = found_or_404(approved, "Host not found or not pending")
    _schedule_host_acceptance_tasks(
        host.id,
        event_services=event_services,
        pack_services=pack_services,
        host_services=host_services,
    )
    return _serialize_host(host, settings_services)


@router.post("/{host_id}/reject", status_code=204)
async def reject_host(host_id: uuid.UUID, host_services: HostServicesDep) -> None:
    async with host_services.session_factory.begin() as db:
        rejected = await host_services.crud.reject_host(db, host_id)
    if not rejected:
        raise HTTPException(status_code=404, detail="Host not found or not pending")


@router.post("", response_model=HostRead, status_code=201)
async def create_host(
    data: HostCreate, host_services: HostServicesDep, settings_services: SettingsServicesDep
) -> dict[str, Any]:
    try:
        async with host_services.session_factory.begin() as db:
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
    host_services: HostServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    pack_services: PackServicesDep,
) -> list[pack_schemas.HostPackDoctorOut]:
    target = await _online_host_target(
        host_services,
        settings_services,
        host_id,
        missing_detail="host not found",
        offline_status=409,
        offline_detail="host must be online to run doctor checks",
    )

    checks = await agent_operations.pack_doctor(
        target.ip,
        target.agent_port,
        pack_id,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )

    async with pack_services.session_factory.begin() as db:
        await pack_services.status.persist_doctor_results(db, host_id, pack_id, checks)

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
    since: datetime | None = None,
    until: datetime | None = None,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 5,
) -> HostResourceTelemetryResponse:
    window_end = until or now_utc()
    window_start = since or (window_end - timedelta(minutes=HOST_TELEMETRY_WINDOW_MINUTES))
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
    host_services: HostServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
) -> dict[str, Any]:
    target = await _online_host_target(
        host_services,
        settings_services,
        host_id,
        missing_detail="Host not found",
        offline_status=400,
        offline_detail="Host must be online to fetch tool status",
    )
    return await get_agent_tool_status(
        target.ip,
        target.agent_port,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )


@router.delete("/{host_id}", status_code=204)
async def delete_host(host_id: uuid.UUID, host_services: HostServicesDep) -> None:
    try:
        async with host_services.session_factory.begin() as db:
            deleted = await host_services.crud.delete_host(db, host_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="Host not found")


async def _fetch_candidates(
    host_services: HostServices, pack_services: PackServices, host_id: uuid.UUID
) -> tuple[HostTarget, tuple[Mapping[str, Any], ...]]:
    """Prepare, then dial. Both discovery reads and the confirm write start here."""
    target = await _load_host_target(host_services, host_id)
    return target, await pack_services.discovery.fetch_pack_candidates(target)


@router.post("/{host_id}/discover", response_model=DiscoveryResult)
async def discover_devices(
    host_id: uuid.UUID,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> DiscoveryResult:
    target, candidates = await _fetch_candidates(host_services, pack_services, host_id)
    async with pack_services.session_factory() as db:
        return await pack_services.discovery.classify_discovery(db, target.host_id, candidates)


@router.get("/{host_id}/intake-candidates", response_model=list[IntakeCandidateRead])
async def intake_candidates(
    host_id: uuid.UUID,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> list[IntakeCandidateRead]:
    target, candidates = await _fetch_candidates(host_services, pack_services, host_id)
    async with pack_services.session_factory() as db:
        return await pack_services.discovery.build_intake_candidates(db, target.host_id, candidates)


@router.post("/{host_id}/discover/confirm", response_model=DiscoveryConfirmResult)
async def confirm_discovery(
    host_id: uuid.UUID,
    data: DiscoveryConfirm,
    host_services: HostServicesDep,
    pack_services: PackServicesDep,
) -> DiscoveryConfirmResult:
    # Confirmation is twice-removed from the agent: it re-dials for fresh
    # validation data and then writes. Prepare and dial first, so the write
    # transaction below opens only once the network call has returned.
    target, candidates = await _fetch_candidates(host_services, pack_services, host_id)
    try:
        # Only a genuinely absent Host row becomes a 404 here. A boot rotation is
        # a recoverable conflict on a host that still exists, so it keeps the 409
        # lane alongside the identity conflict rather than reading as a deletion.
        with convert_missing_row("Host not found"):
            async with pack_services.session_factory.begin() as db:
                return await pack_services.discovery.confirm_discovery(
                    db,
                    target,
                    candidates,
                    data.add_identity_values,
                    data.remove_identity_values,
                )
    except (StaleHostGenerationError, DeviceIdentityConflictError) as exc:
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
    host_services: HostServicesDep,
) -> dict[str, Any]:
    with convert_missing_row("Host not found"):
        async with host_services.session_factory.begin() as db:
            env = await host_services.crud.update_tool_env(db, host_id, body.env)
    return {"env": env}
