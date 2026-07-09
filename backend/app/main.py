import asyncio
import contextlib
import os
import signal
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.config import agent_settings
from app.agent_comm.http_pool import AgentHttpPool, build_agent_basic_auth
from app.analytics import router as analytics
from app.appium_nodes import routers as appium_node_routers
from app.appium_nodes.services.host_sweep import HostSweepLoop, SweepStage
from app.appium_nodes.services.node_viability import device_node_is_viable
from app.auth import dependencies as auth_dependencies
from app.auth import router as auth_router_module
from app.auth import service as auth_service
from app.auth.middleware import StaticPathsAuthMiddleware
from app.composition import compose_app
from app.core import gc_tuning
from app.core.config import DOCS_ENABLED_ENVIRONMENTS
from app.core.config import settings as process_settings
from app.core.database import async_session as session_factory
from app.core.database import engine

# FastAPI resolves these Annotated dependency aliases at runtime for the health/
# metrics endpoints defined in this module, so they must stay at module scope.
from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI resolves Annotated dependency at runtime.
from app.core.errors import register_exception_handlers
from app.core.health import check_liveness, check_readiness
from app.core.leader.advisory import control_plane_leader
from app.core.metrics import CONTENT_TYPE_LATEST, refresh_system_gauges, render_metrics
from app.core.middleware import RequestContextMiddleware
from app.core.observability import (
    configure_logging,
    flush_background_loop_snapshots,
    get_logger,
    stalled_background_loop_names,
)
from app.core.schemas_health import HealthStatusRead, LiveHealthRead
from app.core.shutdown import shutdown_coordinator
from app.core.timeutil import now_utc
from app.devices import routers as device_routers
from app.devices.dependencies import (
    DeviceServicesDep,  # noqa: TC001 - FastAPI resolves Annotated dependency at runtime.
)
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services import (
    data_cleanup,
    fleet_capacity,
    intent_reconciler,
    readiness,
)
from app.devices.services import (
    health as device_health,
)
from app.events import router as events
from app.events.event_bus import EventBus, register_events_gauge_refresher
from app.grid import appium_direct
from app.grid import router as grid
from app.grid import router_internal as grid_router_internal
from app.grid.allocation_reaper import GridAllocationReaperLoop
from app.hosts import router as hosts
from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus
from app.lifecycle import router as lifecycle_router
from app.packs import routers as pack_routers
from app.packs.services.drain import PackDrainLoop
from app.portability import router as portability_router
from app.runs import router as runs_router
from app.runs.service_reaper import RunReaperLoop
from app.sessions import router as sessions_router
from app.sessions.appium_sweep import AppiumSweepLoop
from app.settings import router as settings
from app.settings.dependencies import (
    SettingsServicesDep,  # noqa: TC001 - FastAPI resolves Annotated dependency at runtime.
)
from app.settings.service import SettingsService
from app.verification import router as verification_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.composition import AppServices

configure_logging()

logger = get_logger(__name__)

SHUTDOWN_DRAIN_TIMEOUT_SEC = 30.0
DataCleanupLoop = data_cleanup.DataCleanupLoop
DeviceIntentReconcilerLoop = intent_reconciler.DeviceIntentReconcilerLoop
FleetCapacityLoop = fleet_capacity.FleetCapacityLoop
assess_devices_async = readiness.assess_devices_async
is_ready_for_use_async = readiness.is_ready_for_use_async


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


async def _build_and_start_app_services(
    app: FastAPI,
) -> tuple[EventBus, SettingsService, AgentHttpPool, AppServices]:
    bus = EventBus()
    register_events_gauge_refresher(bus)
    svc = SettingsService()
    pool = AgentHttpPool(agent_auth=build_agent_basic_auth(agent_settings))
    breaker = AgentCircuitBreaker(publisher=bus, settings=svc, session_factory=session_factory)

    app_services = compose_app(
        session_factory=session_factory,
        bus=bus,
        settings_svc=svc,
        http_pool=pool,
        circuit_breaker=breaker,
    )
    app.state.services = app_services

    bus.configure(session_factory=session_factory, engine=engine)
    svc.configure_store_refresh(session_factory)

    # Initialize settings cache from DB before starting background tasks
    async with session_factory() as db:
        await svc.initialize(db)
        await _validate_online_agent_contracts(db)

    pool.configure_limits(
        max_keepalive=svc.get_int("agent.http_pool_max_keepalive"),
        keepalive_expiry=svc.get_int("agent.http_pool_idle_seconds"),
    )
    await pool.reopen()
    bus.register_handler(svc.handle_system_event)
    await bus.start()
    return bus, svc, pool, app_services


def _build_leader_loop_tasks(app_services: AppServices) -> list[asyncio.Task[None]]:
    data_cleanup = DataCleanupLoop(services=app_services.devices)
    fleet_capacity = FleetCapacityLoop(services=app_services.devices)
    intent_reconciler = DeviceIntentReconcilerLoop(services=app_services.devices)
    host_sweep = HostSweepLoop(
        services=app_services.appium_nodes,
        global_stages=(
            SweepStage(
                "connectivity",
                "general.device_check_interval_sec",
                app_services.devices.connectivity.run_connectivity_pass,
            ),
            SweepStage(
                "host_resource_telemetry",
                "general.host_resource_telemetry_interval_sec",
                app_services.hosts.resource_telemetry.poll_once,
            ),
            SweepStage(
                "hardware_telemetry",
                "general.hardware_telemetry_interval_sec",
                app_services.hosts.hardware_telemetry.poll_once,
            ),
            SweepStage(
                "property_refresh",
                "general.property_refresh_interval_sec",
                app_services.devices.property_refresh.refresh_all_properties,
            ),
        ),
    )
    appium_sweep = AppiumSweepLoop(services=app_services.sessions)

    run_reaper = RunReaperLoop(services=app_services.runs)
    allocation_reaper = GridAllocationReaperLoop(services=app_services.grid)
    pack_drain = PackDrainLoop(services=app_services.packs)
    job_worker = app_services.jobs
    background_loop_flush = app_services.background_loop_flush

    _leader_loops: list[tuple[Any, str]] = [
        (host_sweep.run(), "host_sweep_loop"),
        (appium_sweep.run(), "appium_sweep_loop"),
        (job_worker.run(), "durable_job_worker_loop"),
        (run_reaper.run(), "run_reaper_loop"),
        (allocation_reaper.run(), "grid_allocation_reaper_loop"),
        (data_cleanup.run(), "data_cleanup_loop"),
        (fleet_capacity.run(), "fleet_capacity_collector_loop"),
        (pack_drain.run(), "pack_drain_loop"),
        (intent_reconciler.run(), "device_intent_reconciler_loop"),
        (background_loop_flush.run(), "background_loop_flush_loop"),
    ]
    return [asyncio.create_task(coro, name=name) for coro, name in _leader_loops]


SCHEDULER_STALL_GRACE_SEC = 600.0
SCHEDULER_WATCHDOG_INTERVAL_SEC = 60.0
DEFAULT_RESTART_WINDOW_SEC = 120


async def _scheduler_stall_watchdog() -> None:
    """Exit the scheduler when its loops wedge, so compose restart recovers them.

    Restart-based failover: this replaces the old cross-process watcher preempt
    for the realistic stall modes (a loop stuck on a dead await, silent task
    death). A fully wedged event loop starves this task too — that case is
    covered by the Prometheus loop-staleness alerts, not automation.
    """
    while True:
        await asyncio.sleep(SCHEDULER_WATCHDOG_INTERVAL_SEC)
        stalled = stalled_background_loop_names(now=now_utc(), extra_grace_seconds=SCHEDULER_STALL_GRACE_SEC)
        if stalled:
            logger.error(
                "scheduler_loops_stalled",
                stalled=sorted(stalled),
                action="exiting_process_for_supervisor_restart",
            )
            os._exit(70)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    auth_service.validate_process_configuration()
    shutdown_coordinator.reset()

    bus, svc, pool, app_services = await _build_and_start_app_services(app)

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

    if process_settings.run_background_loops:
        if await control_plane_leader.try_acquire(engine):
            tasks = _build_leader_loop_tasks(app_services)
            tasks.append(asyncio.create_task(_scheduler_stall_watchdog(), name="scheduler_stall_watchdog"))
        else:
            logger.warning(
                "background_loops_skipped_lock_held",
                detail="GRIDFLEET_RUN_BACKGROUND_LOOPS is true but another process holds the singleton lock",
            )
    gc_tuning.tune_after_startup()
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
        await svc.shutdown()
        await control_plane_leader.release()
        await bus.shutdown()
        await appium_direct.aclose()
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

app.include_router(auth_router_module.router)
app.include_router(appium_node_routers.admin.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(appium_node_routers.agent_state.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(
    device_routers.bulk.router, dependencies=[Depends(auth_dependencies.require_any_auth)]
)  # Must be before devices.router for /api/devices/bulk/* route precedence
app.include_router(portability_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(appium_node_routers.nodes.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(grid_router_internal.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(hosts.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(sessions_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(verification_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(events.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(device_routers.groups.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(runs_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(analytics.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(lifecycle_router.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(settings.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.export.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.catalog.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.uploads.router, dependencies=[Depends(auth_dependencies.require_any_auth)])
app.include_router(pack_routers.agent_state.router, dependencies=[Depends(auth_dependencies.require_any_auth)])


@app.get("/health/live", response_model=LiveHealthRead)
async def live_health() -> dict[str, str]:
    return await check_liveness()


@app.get("/health/ready", response_model=HealthStatusRead)
async def ready_health(db: DbDep, settings_services: SettingsServicesDep) -> JSONResponse:
    payload, status_code = await check_readiness(
        db, settings=settings_services.service, fail_on_stalled_loops=process_settings.run_background_loops
    )
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/api/health", response_model=HealthStatusRead)
async def health(db: DbDep, settings_services: SettingsServicesDep) -> JSONResponse:
    payload, status_code = await check_readiness(
        db, settings=settings_services.service, fail_on_stalled_loops=process_settings.run_background_loops
    )
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/metrics")
async def metrics(db: DbDep) -> Response:
    await refresh_system_gauges(db)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/availability", dependencies=[Depends(auth_dependencies.require_any_auth)])
async def check_availability(
    db: DbDep,
    device_services: DeviceServicesDep,
    platform_id: Annotated[str, Query()],
    count: Annotated[int, Query(ge=1)] = 1,
) -> dict[str, Any]:
    # Mirror the run allocator's eligibility gates (``_find_matching_devices``):
    # reserved devices and non-viable nodes never match an allocation, and both
    # are orthogonal to ``operational_state`` — counting them here would report
    # capacity the allocator cannot use.
    available_devices = await device_services.crud.list_devices_by_filters(
        db, DeviceQueryFilters(platform_id=platform_id, status="available", reserved=False)
    )
    readiness_map = await assess_devices_async(db, available_devices)
    matched = sum(
        1
        for device in available_devices
        if not device.review_required
        and device_node_is_viable(
            device,
            now=now_utc(),
            restart_window_sec=DEFAULT_RESTART_WINDOW_SEC,
        )
        and readiness_map[device.id].readiness_state == "verified"
        and device_health.device_allows_allocation(device)
    )
    return {
        "available": matched >= count,
        "requested": count,
        "matched": matched,
        "platform_id": platform_id,
    }
