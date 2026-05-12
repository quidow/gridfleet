import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import freeze_background_loops_enabled
from app.database import async_session as session_factory
from app.database import engine, get_db
from app.errors import register_exception_handlers
from app.health import check_liveness, check_readiness
from app.metrics import CONTENT_TYPE_LATEST, refresh_system_gauges, render_metrics
from app.middleware import RequestContextMiddleware
from app.models.host import Host, HostStatus
from app.observability import configure_logging, get_logger
from app.routers import (
    admin_appium_nodes,
    agent_driver_packs,
    analytics,
    auth,
    bulk,
    device_groups,
    devices,
    driver_pack_authoring,
    driver_pack_export,
    driver_pack_templates,
    driver_pack_uploads,
    driver_packs,
    events,
    grid,
    host_driver_pack_features,
    host_terminal,
    hosts,
    lifecycle,
    nodes,
    plugins,
    runs,
    sessions,
    settings,
    webhooks,
)
from app.schemas.health import HealthStatusRead, LiveHealthRead
from app.services import auth as auth_service
from app.services import device_health, device_service, host_service, webhook_dispatcher
from app.services.agent_http_pool import agent_http_pool
from app.services.appium_reconciler import appium_reconciler_loop
from app.services.control_plane_leader import control_plane_leader
from app.services.control_plane_leader_keepalive import control_plane_leader_keepalive_loop
from app.services.control_plane_leader_watcher import control_plane_leader_watcher_loop
from app.services.data_cleanup import data_cleanup_loop
from app.services.device_connectivity import device_connectivity_loop
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import event_bus
from app.services.fleet_capacity import fleet_capacity_collector_loop
from app.services.grid_node_run_id_reconciler import grid_node_run_id_reconciler_loop
from app.services.grid_service import close as close_grid_service_client
from app.services.hardware_telemetry import hardware_telemetry_loop
from app.services.heartbeat import (
    heartbeat_loop,
    shutdown_background_tasks,
)
from app.services.host_resource_telemetry import host_resource_telemetry_loop
from app.services.job_queue import durable_job_worker_loop
from app.services.node_health import node_health_loop
from app.services.pack_drain import pack_drain_loop
from app.services.property_refresh import property_refresh_loop
from app.services.run_reaper import run_reaper_loop
from app.services.session_sync import session_sync_loop
from app.services.session_viability import close as close_session_viability_client
from app.services.session_viability import session_viability_loop
from app.services.settings_service import settings_service, validate_leader_keepalive_settings
from app.shutdown import shutdown_coordinator

configure_logging()

logger = get_logger(__name__)

SHUTDOWN_DRAIN_TIMEOUT_SEC = 30.0


def _freeze_background_loops() -> bool:
    """Skip all leader-owned background loops when truthy.

    Set via ``GRIDFLEET_FREEZE_BACKGROUND_LOOPS`` to keep a seeded demo database in
    a frozen state — no heartbeat/health/reaper mutations marking hosts and
    devices offline.
    """
    return freeze_background_loops_enabled()


def _validate_leader_keepalive_settings() -> None:
    keepalive_interval_sec = int(settings_service.get("general.leader_keepalive_interval_sec"))
    stale_threshold_sec = int(settings_service.get("general.leader_stale_threshold_sec"))
    error = validate_leader_keepalive_settings(
        keepalive_interval_sec=keepalive_interval_sec,
        stale_threshold_sec=stale_threshold_sec,
    )
    if error:
        raise RuntimeError(f"Misconfigured leader keepalive settings: {error}")


async def _validate_online_agent_contracts(db: AsyncSession) -> None:
    result = await db.execute(select(Host).where(Host.status == HostStatus.online).order_by(Host.hostname))
    hosts = result.scalars().all()
    for host in hosts:
        try:
            host_service.validate_orchestration_contract(
                host.capabilities,
                host_label=f"{host.hostname} ({host.id})",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc


async def _cancel_and_wait_for_tasks(tasks: list[asyncio.Task[None]], *, label: str) -> None:
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            logger.error(
                "%s task %s failed during shutdown",
                label,
                task.get_name(),
                exc_info=(type(result), result, result.__traceback__),
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    auth_service.validate_process_configuration()
    shutdown_coordinator.reset()

    event_bus.configure(session_factory=session_factory, engine=engine)
    settings_service.configure_store_refresh(session_factory)
    webhook_dispatcher.configure(session_factory)

    # Initialize settings cache from DB before starting background tasks
    async with session_factory() as db:
        await settings_service.initialize(db)
        await _validate_online_agent_contracts(db)
    _validate_leader_keepalive_settings()

    await agent_http_pool.reopen()
    event_bus.register_handler(settings_service.handle_system_event)
    event_bus.register_handler(webhook_dispatcher.handle_system_event)
    await event_bus.start()

    tasks: list[asyncio.Task[None]] = []
    loop = asyncio.get_running_loop()
    signal_tasks: set[asyncio.Task[None]] = set()

    def _begin_shutdown() -> None:
        task = asyncio.create_task(shutdown_coordinator.begin_shutdown())
        signal_tasks.add(task)
        task.add_done_callback(signal_tasks.discard)

    registered_signals: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, _begin_shutdown)
            registered_signals.append(signum)

    freeze = _freeze_background_loops()
    watcher_task: asyncio.Task[None] | None = None

    if freeze:
        logger.warning(
            "GRIDFLEET_FREEZE_BACKGROUND_LOOPS is set; skipping all leader-owned "
            "background loops (heartbeat, health, reaper, telemetry, webhook, "
            "cleanup, capacity). State in the database will not be mutated by "
            "the backend. Use this only for frozen demo databases."
        )
    if not freeze:
        if await control_plane_leader.try_acquire(engine):
            tasks = [
                asyncio.create_task(control_plane_leader_keepalive_loop(), name="control_plane_leader_keepalive"),
                asyncio.create_task(heartbeat_loop(), name="heartbeat_loop"),
                asyncio.create_task(session_sync_loop(), name="session_sync_loop"),
                asyncio.create_task(node_health_loop(), name="node_health_loop"),
                asyncio.create_task(device_connectivity_loop(), name="device_connectivity_loop"),
                asyncio.create_task(property_refresh_loop(), name="property_refresh_loop"),
                asyncio.create_task(hardware_telemetry_loop(), name="hardware_telemetry_loop"),
                asyncio.create_task(host_resource_telemetry_loop(), name="host_resource_telemetry_loop"),
                asyncio.create_task(durable_job_worker_loop(session_factory), name="durable_job_worker_loop"),
                asyncio.create_task(
                    webhook_dispatcher.webhook_delivery_loop(session_factory),
                    name="webhook_dispatcher.webhook_delivery_loop",
                ),
                asyncio.create_task(run_reaper_loop(), name="run_reaper_loop"),
                asyncio.create_task(data_cleanup_loop(), name="data_cleanup_loop"),
                asyncio.create_task(session_viability_loop(), name="session_viability_loop"),
                asyncio.create_task(fleet_capacity_collector_loop(), name="fleet_capacity_collector_loop"),
                asyncio.create_task(pack_drain_loop(), name="pack_drain_loop"),
                asyncio.create_task(appium_reconciler_loop(), name="appium_reconciler_loop"),
                asyncio.create_task(grid_node_run_id_reconciler_loop(), name="grid_node_run_id_reconciler_loop"),
            ]
        watcher_task = asyncio.create_task(
            control_plane_leader_watcher_loop(),
            name="control_plane_leader_watcher",
        )
    try:
        yield
    finally:
        await shutdown_coordinator.begin_shutdown()
        await shutdown_coordinator.wait_for_drain(SHUTDOWN_DRAIN_TIMEOUT_SEC)
        await _cancel_and_wait_for_tasks(tasks, label="background")
        if watcher_task is not None:
            await _cancel_and_wait_for_tasks([watcher_task], label="leader watcher")
        await shutdown_background_tasks()
        await settings_service.shutdown()
        await control_plane_leader.release()
        await event_bus.shutdown()
        await agent_http_pool.close()
        await close_grid_service_client()
        await close_session_viability_client()
        await engine.dispose()
        pending_signal_tasks = list(signal_tasks)
        await _cancel_and_wait_for_tasks(pending_signal_tasks, label="signal")
        for signum in registered_signals:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


app = FastAPI(title="GridFleet", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)

app.include_router(auth.router)
app.include_router(admin_appium_nodes.router)
app.include_router(bulk.router)  # Must be before devices.router for /api/devices/bulk/* route precedence
app.include_router(devices.router)
app.include_router(nodes.router)
app.include_router(grid.router)
app.include_router(hosts.router)
app.include_router(host_terminal.router)
app.include_router(sessions.router)
app.include_router(events.router)
app.include_router(webhooks.router)
app.include_router(device_groups.router)
app.include_router(runs.router)
app.include_router(plugins.router)
app.include_router(analytics.router)
app.include_router(lifecycle.router)
app.include_router(settings.router)
app.include_router(driver_pack_authoring.router)
app.include_router(driver_pack_templates.router)
app.include_router(driver_pack_export.router)
app.include_router(driver_packs.router)
app.include_router(driver_pack_uploads.router)
app.include_router(host_driver_pack_features.router)
app.include_router(agent_driver_packs.router)


@app.get("/health/live", response_model=LiveHealthRead)
async def live_health() -> dict[str, str]:
    return await check_liveness()


@app.get("/health/ready", response_model=HealthStatusRead)
async def ready_health(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    payload, status_code = await check_readiness(db)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/health", response_model=HealthStatusRead)
async def health(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    payload, status_code = await check_readiness(db)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)) -> Response:
    await refresh_system_gauges(db)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/availability")
async def check_availability(
    platform_id: str = Query(...),
    count: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    available_devices = await device_service.list_devices(db, platform_id=platform_id, status="available")
    matched = 0
    for device in available_devices:
        ready = await is_ready_for_use_async(db, device)
        health_allows_allocation = device_health.device_allows_allocation(device)
        if ready and health_allows_allocation:
            matched += 1
    return {
        "available": matched >= count,
        "requested": count,
        "matched": matched,
        "platform_id": platform_id,
    }
