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

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.config import agent_settings
from app.agent_comm.http_pool import AgentHttpPool, build_agent_basic_auth
from app.analytics import router as analytics
from app.appium_nodes import exception_handlers as appium_node_exception_handlers
from app.appium_nodes import routers as appium_node_routers
from app.appium_nodes.services.heartbeat import HeartbeatLoop, shutdown_background_tasks
from app.appium_nodes.services.node_health import NodeHealthLoop
from app.appium_nodes.services.reconciler import AppiumReconcilerLoop
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
from app.core.leader.advisory import control_plane_leader
from app.core.metrics import CONTENT_TYPE_LATEST, refresh_system_gauges, render_metrics
from app.core.middleware import RequestContextMiddleware
from app.core.observability import (
    configure_logging,
    flush_background_loop_snapshots,
    get_logger,
)
from app.core.protocols import SettingsReader
from app.core.schemas_health import HealthStatusRead, LiveHealthRead
from app.core.shutdown import shutdown_coordinator
from app.devices import routers as device_routers
from app.devices import services as device_services
from app.devices.dependencies import DeviceServicesDep
from app.devices.services import state_write_guard
from app.diagnostics import router as diagnostics_router
from app.events import router as events
from app.events.event_bus import EventBus, register_events_gauge_refresher
from app.grid import router as grid
from app.grid import router_internal as grid_router_internal
from app.grid.allocation_reaper import GridAllocationReaperLoop
from app.hosts import router as hosts
from app.hosts import router_agent_logs as host_agent_logs
from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus
from app.hosts.service_hardware_telemetry import HardwareTelemetryLoop
from app.hosts.service_resource_telemetry import HostResourceTelemetryLoop
from app.lifecycle import router as lifecycle_router
from app.packs import routers as pack_routers
from app.packs.services.drain import PackDrainLoop
from app.plugins import router as plugins
from app.portability import router as portability_router
from app.runs import router as runs_router
from app.runs.service_reaper import RunReaperLoop
from app.sessions import router as sessions_router
from app.sessions.service_sync import SessionSyncLoop
from app.sessions.service_viability import SessionViabilityLoop
from app.settings import router as settings
from app.settings import validate_leader_keepalive_settings
from app.settings.dependencies import SettingsServicesDep
from app.settings.service import SettingsService
from app.verification import router as verification_router
from app.webhooks import router as webhooks
from app.webhooks.dispatcher import WebhookDeliveryLoop

configure_logging()

logger = get_logger(__name__)

SHUTDOWN_DRAIN_TIMEOUT_SEC = 30.0
DataCleanupLoop = device_services.data_cleanup.DataCleanupLoop
DeviceConnectivityLoop = device_services.connectivity.DeviceConnectivityLoop
device_health = device_services.health
DeviceIntentReconcilerLoop = device_services.intent_reconciler.DeviceIntentReconcilerLoop
FleetCapacityLoop = device_services.fleet_capacity.FleetCapacityLoop
assess_devices_async = device_services.readiness.assess_devices_async
is_ready_for_use_async = device_services.readiness.is_ready_for_use_async
PropertyRefreshLoop = device_services.property_refresh.PropertyRefreshLoop


def _validate_leader_keepalive_settings(*, settings: SettingsReader) -> None:
    keepalive_interval_sec = int(settings.get("general.leader_keepalive_interval_sec"))
    stale_threshold_sec = int(settings.get("general.leader_stale_threshold_sec"))
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
    register_events_gauge_refresher(bus)
    svc = SettingsService()
    pool = AgentHttpPool(agent_auth=build_agent_basic_auth(agent_settings))
    breaker = AgentCircuitBreaker(publisher=bus, settings=svc, session_factory=session_factory)

    app_services = compose_app(
        engine=engine,
        session_factory=session_factory,
        bus=bus,
        settings_svc=svc,
        http_pool=pool,
        circuit_breaker=breaker,
        control_plane_leader=control_plane_leader,
    )
    app.state.services = app_services

    bus.configure(session_factory=session_factory, engine=engine)
    svc.configure_store_refresh(session_factory)

    # Initialize settings cache from DB before starting background tasks
    async with session_factory() as db:
        await svc.initialize(db)
        await _validate_online_agent_contracts(db)
    _validate_leader_keepalive_settings(settings=svc)

    await pool.reopen()
    bus.register_handler(svc.handle_system_event)
    bus.register_handler(app_services.webhooks.dispatch.handle_system_event)
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
        connectivity_loop = DeviceConnectivityLoop(services=app_services.devices)
        data_cleanup = DataCleanupLoop(services=app_services.devices)
        fleet_capacity = FleetCapacityLoop(services=app_services.devices)
        intent_reconciler = DeviceIntentReconcilerLoop(services=app_services.devices)
        property_refresh = PropertyRefreshLoop(services=app_services.devices)
        heartbeat = HeartbeatLoop(services=app_services.appium_nodes)
        node_health = NodeHealthLoop(services=app_services.appium_nodes)
        appium_reconciler = AppiumReconcilerLoop(services=app_services.appium_nodes)
        session_sync = SessionSyncLoop(services=app_services.sessions)
        session_viability = SessionViabilityLoop(services=app_services.sessions)
        hardware_telemetry = HardwareTelemetryLoop(services=app_services.hosts)
        host_resource_telemetry = HostResourceTelemetryLoop(services=app_services.hosts)

        run_reaper = RunReaperLoop(services=app_services.runs)
        allocation_reaper = GridAllocationReaperLoop(services=app_services.grid)
        pack_drain = PackDrainLoop(services=app_services.packs)
        job_worker = app_services.jobs
        webhook_delivery = WebhookDeliveryLoop(services=app_services.webhooks)
        background_loop_flush = app_services.background_loop_flush

        _leader_loops: list[tuple[Any, str]] = [
            (app_services.leader_keepalive.run(), "control_plane_leader_keepalive"),
            (heartbeat.run(), "heartbeat_loop"),
            (session_sync.run(), "session_sync_loop"),
            (node_health.run(), "node_health_loop"),
            (connectivity_loop.run(), "device_connectivity_loop"),
            (property_refresh.run(), "property_refresh_loop"),
            (hardware_telemetry.run(), "hardware_telemetry_loop"),
            (host_resource_telemetry.run(), "host_resource_telemetry_loop"),
            (job_worker.run(), "durable_job_worker_loop"),
            (webhook_delivery.run(), "webhook_dispatcher.webhook_delivery_loop"),
            (run_reaper.run(), "run_reaper_loop"),
            (allocation_reaper.run(), "grid_allocation_reaper_loop"),
            (data_cleanup.run(), "data_cleanup_loop"),
            (session_viability.run(), "session_viability_loop"),
            (fleet_capacity.run(), "fleet_capacity_collector_loop"),
            (pack_drain.run(), "pack_drain_loop"),
            (appium_reconciler.run(), "appium_reconciler_loop"),
            (intent_reconciler.run(), "device_intent_reconciler_loop"),
            (background_loop_flush.run(), "background_loop_flush_loop"),
        ]
        tasks = [asyncio.create_task(coro, name=name) for coro, name in _leader_loops]
    watcher_task = asyncio.create_task(
        app_services.leader_watcher.run(),
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
app.include_router(portability_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(diagnostics_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(appium_node_routers.nodes.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid_router_internal.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(hosts.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(sessions_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(verification_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(events.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(webhooks.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.groups.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(runs_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(plugins.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(analytics.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(lifecycle_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
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
async def ready_health(db: DbDep, settings_services: SettingsServicesDep) -> JSONResponse:
    payload, status_code = await check_readiness(db, settings=settings_services.service)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/health", response_model=HealthStatusRead)
async def health(db: DbDep, settings_services: SettingsServicesDep) -> JSONResponse:
    payload, status_code = await check_readiness(db, settings=settings_services.service)
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/metrics")
async def metrics(db: DbDep) -> Response:
    await refresh_system_gauges(db)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/availability", dependencies=[Depends(auth_dependencies.require_any_auth)])
async def check_availability(
    db: DbDep,
    device_services: DeviceServicesDep,
    platform_id: str = Query(...),
    count: int = Query(1, ge=1),
) -> dict[str, Any]:
    available_devices = await device_services.crud.list_devices(db, platform_id=platform_id, status="available")
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
