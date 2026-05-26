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

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.http_pool import AgentHttpPool
from app.analytics import router as analytics
from app.appium_nodes import exception_handlers as appium_node_exception_handlers
from app.appium_nodes import routers as appium_node_routers
from app.appium_nodes import services as appium_node_services
from app.auth import dependencies as auth_dependencies
from app.auth import router as auth_router_module
from app.auth import service as auth_service
from app.auth.middleware import StaticPathsAuthMiddleware
from app.composition import compose_app
from app.core.config import DOCS_ENABLED_ENVIRONMENTS
from app.core.config import settings as process_settings
from app.core.database import async_session as session_factory
from app.core.database import engine
from app.core.dependencies import DbDep
from app.core.errors import register_exception_handlers
from app.core.health import check_liveness, check_readiness
from app.core.leader import register_settings_provider
from app.core.leader.advisory import control_plane_leader
from app.core.leader.keepalive import control_plane_leader_keepalive_loop
from app.core.leader.watcher import control_plane_leader_watcher_loop
from app.core.metrics import CONTENT_TYPE_LATEST, refresh_system_gauges, render_metrics
from app.core.middleware import RequestContextMiddleware
from app.core.observability import (
    background_loop_flush_loop,
    configure_logging,
    flush_background_loop_snapshots,
    get_logger,
)
from app.core.schemas_health import HealthStatusRead, LiveHealthRead
from app.core.shutdown import shutdown_coordinator
from app.devices import routers as device_routers
from app.devices import services as device_services
from app.devices.services import state_write_guard
from app.events import router as events
from app.events.event_bus import EventBus
from app.grid import router as grid
from app.grid import service as grid_service
from app.grid.event_bus_loop import event_bus_subscriber_loop
from app.hosts import router as hosts
from app.hosts import router_agent_logs as host_agent_logs
from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus
from app.jobs import queue as job_queue
from app.packs import routers as pack_routers
from app.packs import services as pack_services
from app.plugins import router as plugins
from app.runs import router as runs_router
from app.runs import service_reaper as run_service_reaper
from app.sessions import router as sessions_router
from app.sessions import service_sync as session_service_sync
from app.sessions import service_viability as session_service_viability
from app.settings import router as settings
from app.settings import settings_service, validate_leader_keepalive_settings
from app.settings.service import SettingsService
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
assess_devices_async = device_services.readiness.assess_devices_async
is_ready_for_use_async = device_services.readiness.is_ready_for_use_async
property_refresh_loop = device_services.property_refresh.property_refresh_loop
run_reaper_loop = run_service_reaper.run_reaper_loop
session_sync_loop = session_service_sync.session_sync_loop
session_viability_loop = session_service_viability.session_viability_loop
close_session_viability_client = session_service_viability.close


async def hardware_telemetry_loop() -> None:
    service_hardware_telemetry = importlib.import_module("app.hosts.service_hardware_telemetry")
    await service_hardware_telemetry.hardware_telemetry_loop()


async def host_resource_telemetry_loop() -> None:
    service_resource_telemetry = importlib.import_module("app.hosts.service_resource_telemetry")
    await service_resource_telemetry.host_resource_telemetry_loop()


async def pack_drain_loop() -> None:
    await pack_services.drain.pack_drain_loop()


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
    state_write_guard.register()
    auth_service.validate_process_configuration()
    shutdown_coordinator.reset()

    bus = EventBus()
    svc = SettingsService()
    pool = AgentHttpPool()
    breaker = AgentCircuitBreaker(publisher=bus)

    app_services = compose_app(
        engine=engine,
        session_factory=session_factory,
        bus=bus,
        settings_svc=svc,
        http_pool=pool,
        circuit_breaker=breaker,
    )
    app.state.services = app_services

    bus.configure(session_factory=session_factory, engine=engine)
    svc.configure_store_refresh(session_factory)
    webhook_dispatcher.configure(session_factory)

    # Initialize settings cache from DB before starting background tasks
    async with session_factory() as db:
        await svc.initialize(db)
        await _validate_online_agent_contracts(db)
    register_settings_provider(svc.get)
    _validate_leader_keepalive_settings()

    await pool.reopen()
    bus.register_handler(svc.handle_system_event)
    bus.register_handler(webhook_dispatcher.handle_system_event)
    await bus.start()

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

    watcher_task: asyncio.Task[None] | None = None

    if await control_plane_leader.try_acquire(engine):
        _leader_loops: list[tuple[Any, str]] = [
            (control_plane_leader_keepalive_loop(), "control_plane_leader_keepalive"),
            (heartbeat_loop(), "heartbeat_loop"),
            (session_sync_loop(), "session_sync_loop"),
            (event_bus_subscriber_loop(), "grid_event_bus_subscriber_loop"),
            (node_health_loop(), "node_health_loop"),
            (device_connectivity_loop(), "device_connectivity_loop"),
            (property_refresh_loop(), "property_refresh_loop"),
            (hardware_telemetry_loop(), "hardware_telemetry_loop"),
            (host_resource_telemetry_loop(), "host_resource_telemetry_loop"),
            (job_queue.durable_job_worker_loop(session_factory, publisher=bus), "durable_job_worker_loop"),
            (webhook_dispatcher.webhook_delivery_loop(session_factory), "webhook_dispatcher.webhook_delivery_loop"),
            (run_reaper_loop(), "run_reaper_loop"),
            (data_cleanup_loop(publisher=bus), "data_cleanup_loop"),
            (session_viability_loop(), "session_viability_loop"),
            (fleet_capacity_collector_loop(), "fleet_capacity_collector_loop"),
            (pack_drain_loop(), "pack_drain_loop"),
            (appium_reconciler_loop(), "appium_reconciler_loop"),
            (device_intent_reconciler_loop(), "device_intent_reconciler_loop"),
            (background_loop_flush_loop(session_factory), "background_loop_flush_loop"),
        ]
        tasks = [asyncio.create_task(coro, name=name) for coro, name in _leader_loops]
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
        # Persist the last in-memory heartbeat snapshot so operators see fresh
        # values immediately after a clean restart, instead of waiting for the
        # first cycle of the new process to write rows.
        try:
            await flush_background_loop_snapshots(session_factory)
        except Exception:
            logger.exception("background_loop_final_flush_failed")
        if watcher_task is not None:
            await _cancel_and_wait_for_tasks([watcher_task], label="leader watcher")
        await shutdown_background_tasks()
        await svc.shutdown()
        await control_plane_leader.release()
        await bus.shutdown()
        await pool.close()
        await grid_service.close()
        await close_session_viability_client()
        await engine.dispose()
        pending_signal_tasks = list(signal_tasks)
        await _cancel_and_wait_for_tasks(pending_signal_tasks, label="signal")
        for signum in registered_signals:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


def _fastapi_app_kwargs() -> dict[str, Any]:
    app_kwargs: dict[str, Any] = {"title": "GridFleet", "version": "0.1.0", "lifespan": lifespan}
    if process_settings.environment not in DOCS_ENABLED_ENVIRONMENTS:
        app_kwargs["openapi_url"] = None
    return app_kwargs


app = FastAPI(**_fastapi_app_kwargs())
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
app.include_router(
    device_routers.portability.router,
    dependencies=[Depends(auth_dependencies.require_any_auth)],
)  # Must be before catalog for /api/devices/export route precedence over /{device_id}
app.include_router(
    device_routers.inventory.router,
    dependencies=[Depends(auth_dependencies.require_any_auth)],
)  # Must be before catalog for /api/devices/inventory route precedence over /{device_id}
app.include_router(device_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(
    device_routers.diagnostics.router,
    prefix="/api/devices",
    tags=["devices"],
    dependencies=[Depends(auth_dependencies.require_any_auth)],
)
app.include_router(appium_node_routers.nodes.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(hosts.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(sessions_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(events.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(webhooks.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.groups.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(runs_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(plugins.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(analytics.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(
    device_routers.lifecycle_incidents.router, dependencies=[Depends(auth_dependencies.require_any_auth)]
)
app.include_router(settings.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.export.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.uploads.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.host_features.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.agent_state.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(host_agent_logs.router, dependencies=[Depends(auth_dependencies.require_any_auth)])


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
    readiness_map = await assess_devices_async(db, available_devices)
    matched = sum(
        1
        for device in available_devices
        if readiness_map[device.id].readiness_state == "verified" and device_health.device_allows_allocation(device)
    )
    return {
        "available": matched >= count,
        "requested": count,
        "matched": matched,
        "platform_id": platform_id,
    }
