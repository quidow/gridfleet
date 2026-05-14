import asyncio
import contextlib
import importlib
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import router as analytics
from app.appium_nodes import exception_handlers as appium_node_exception_handlers
from app.appium_nodes import routers as appium_node_routers
from app.appium_nodes import services as appium_node_services
from app.auth import dependencies as auth_dependencies
from app.auth import router as auth_router_module
from app.auth import service as auth_service
from app.config import freeze_background_loops_enabled
from app.core.metrics import refresh_system_gauges
from app.core.schemas_health import HealthStatusRead, LiveHealthRead
from app.database import async_session as session_factory
from app.database import engine
from app.dependencies import DbDep
from app.devices import routers as device_routers
from app.devices import services as device_services
from app.errors import register_exception_handlers
from app.events import event_bus
from app.events import router as events
from app.grid import router as grid
from app.grid import service as grid_service
from app.health import check_liveness, check_readiness
from app.hosts import router as hosts
from app.hosts import router_terminal as host_terminal
from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus
from app.jobs import queue as job_queue
from app.metrics import CONTENT_TYPE_LATEST, render_metrics
from app.middleware import RequestContextMiddleware, StaticPathsAuthMiddleware
from app.observability import configure_logging, get_logger
from app.packs import routers as pack_routers
from app.packs import services as pack_services
from app.plugins import router as plugins
from app.routers import runs
from app.services.control_plane_leader import control_plane_leader
from app.services.control_plane_leader_keepalive import control_plane_leader_keepalive_loop
from app.services.control_plane_leader_watcher import control_plane_leader_watcher_loop
from app.services.run_reaper import run_reaper_loop
from app.sessions import router as sessions_router
from app.sessions import service_sync as session_service_sync
from app.sessions import service_viability as session_service_viability
from app.settings import router as settings
from app.settings import settings_service, validate_leader_keepalive_settings
from app.shutdown import shutdown_coordinator
from app.webhooks import dispatcher as webhook_dispatcher
from app.webhooks import router as webhooks

configure_logging()

logger = get_logger(__name__)

SHUTDOWN_DRAIN_TIMEOUT_SEC = 30.0
appium_reconciler_loop = appium_node_services.reconciler.appium_reconciler_loop
heartbeat_loop = appium_node_services.heartbeat.heartbeat_loop
node_health_loop = appium_node_services.node_health.node_health_loop
shutdown_background_tasks = appium_node_services.heartbeat.shutdown_background_tasks
data_cleanup_loop = device_services.data_cleanup.data_cleanup_loop
device_connectivity_loop = device_services.connectivity.device_connectivity_loop
device_health = device_services.health
device_intent_reconciler_loop = device_services.intent_reconciler.device_intent_reconciler_loop
device_service = device_services.service
fleet_capacity_collector_loop = device_services.fleet_capacity.fleet_capacity_collector_loop
is_ready_for_use_async = device_services.readiness.is_ready_for_use_async
property_refresh_loop = device_services.property_refresh.property_refresh_loop
session_sync_loop = session_service_sync.session_sync_loop
session_viability_loop = session_service_viability.session_viability_loop
close_session_viability_client = session_service_viability.close


async def _reopen_agent_http_pool() -> None:
    agent_http_pool_module = importlib.import_module("app.agent_comm.http_pool")
    await agent_http_pool_module.agent_http_pool.reopen()


async def _close_agent_http_pool() -> None:
    agent_http_pool_module = importlib.import_module("app.agent_comm.http_pool")
    await agent_http_pool_module.agent_http_pool.close()


async def hardware_telemetry_loop() -> None:
    service_hardware_telemetry = importlib.import_module("app.hosts.service_hardware_telemetry")
    await service_hardware_telemetry.hardware_telemetry_loop()


async def host_resource_telemetry_loop() -> None:
    service_resource_telemetry = importlib.import_module("app.hosts.service_resource_telemetry")
    await service_resource_telemetry.host_resource_telemetry_loop()


async def pack_drain_loop() -> None:
    await pack_services.drain.pack_drain_loop()


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
    downgraded = False
    for host in hosts:
        try:
            host_service.validate_orchestration_contract(
                host.capabilities,
                host_label=f"{host.hostname} ({host.id})",
            )
        except ValueError as exc:
            logger.warning(
                "host_orchestration_contract_unsupported_marking_offline",
                host_id=str(host.id),
                hostname=host.hostname,
                reason=str(exc),
            )
            host.status = HostStatus.offline
            downgraded = True
    if downgraded:
        await db.commit()


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

    await _reopen_agent_http_pool()
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
                asyncio.create_task(job_queue.durable_job_worker_loop(session_factory), name="durable_job_worker_loop"),
                asyncio.create_task(
                    webhook_dispatcher.webhook_delivery_loop(session_factory),
                    name="webhook_dispatcher.webhook_delivery_loop",
                ),
                asyncio.create_task(run_reaper_loop(), name="run_reaper_loop"),
                asyncio.create_task(data_cleanup_loop(), name="data_cleanup_loop"),
                asyncio.create_task(session_viability_loop(), name="session_viability_loop"),
                asyncio.create_task(fleet_capacity_collector_loop(), name="fleet_capacity_collector_loop"),
                asyncio.create_task(pack_drain_loop(), name="pack_drain_loop"),
                asyncio.create_task(
                    appium_reconciler_loop(),
                    name="appium_reconciler_loop",
                ),
                asyncio.create_task(device_intent_reconciler_loop(), name="device_intent_reconciler_loop"),
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
        await _close_agent_http_pool()
        await grid_service.close()
        await close_session_viability_client()
        await engine.dispose()
        pending_signal_tasks = list(signal_tasks)
        await _cancel_and_wait_for_tasks(pending_signal_tasks, label="signal")
        for signum in registered_signals:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


app = FastAPI(title="GridFleet", version="0.1.0", lifespan=lifespan)
# Starlette installs middlewares in reverse-add order: the most recently added
# wraps the previous one. We want RequestContextMiddleware on the outside so it
# binds request_id, error envelopes, and metrics around all responses — including
# the 401s emitted by StaticPathsAuthMiddleware.
app.add_middleware(StaticPathsAuthMiddleware)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)
appium_node_exception_handlers.register(app)

app.include_router(auth_router_module.router)
app.include_router(appium_node_routers.admin.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(
    device_routers.bulk.router, dependencies=[Depends(auth_dependencies.require_any_auth)]
)  # Must be before devices.router for /api/devices/bulk/* route precedence
app.include_router(device_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(appium_node_routers.nodes.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(hosts.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(host_terminal.router)  # WebSocket-only; auth handled inside the WS handler
app.include_router(sessions_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(events.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(webhooks.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.groups.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(runs.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(plugins.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(analytics.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(
    device_routers.lifecycle_incidents.router, dependencies=[Depends(auth_dependencies.require_any_auth)]
)
app.include_router(settings.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.authoring.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.templates.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.export.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.uploads.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.host_features.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.agent_state.router, dependencies=[Depends(auth_dependencies.require_any_auth)])


@app.get("/health/live", response_model=LiveHealthRead)
async def live_health() -> dict[str, str]:
    return await check_liveness()


@app.get("/health/ready", response_model=HealthStatusRead)
async def ready_health(db: DbDep) -> JSONResponse:
    payload, status_code = await check_readiness(db)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/health", response_model=HealthStatusRead)
async def health(db: DbDep) -> JSONResponse:
    payload, status_code = await check_readiness(db)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/metrics")
async def metrics(db: DbDep) -> Response:
    await refresh_system_gauges(db)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/availability", dependencies=[Depends(auth_dependencies.require_any_auth)])
async def check_availability(
    db: DbDep,
    platform_id: str = Query(...),
    count: int = Query(1, ge=1),
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
