"""Composition root — the ONLY module that knows concrete types.

All domain modules depend on Protocols. This module wires the real
implementations. Called once from app/main.py lifespan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.agent_comm.circuit_breaker import AgentCircuitBreaker
    from app.agent_comm.http_pool import AgentHttpPool
    from app.core.leader.advisory import ControlPlaneLeader
    from app.events.event_bus import EventBus
    from app.settings.service import SettingsService

from app.agent_comm.services_container import AgentCommServices
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.leader.keepalive import LeaderKeepaliveLoop
from app.core.leader.watcher import LeaderWatcherLoop
from app.core.observability import BackgroundLoopFlushLoop
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.state import DeviceStateService
from app.devices.services_container import DeviceServices
from app.events.services_container import EventServices
from app.grid.service import GridService
from app.grid.services_container import GridServices
from app.hosts.service import HostCrudService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.services_container import HostServices
from app.jobs.queue import DurableJobService, DurableJobWorkerLoop
from app.packs import packs_settings
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.release import PackReleaseService
from app.packs.services.service import PackCatalogService
from app.packs.services.status import PackStatusService
from app.packs.services.storage import PackStorageService
from app.packs.services_container import PackServices
from app.plugins.service import PluginService
from app.plugins.services_container import PluginServices
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.services_container import RunServices
from app.sessions.service import SessionCrudService
from app.sessions.service_sync import SessionSyncService
from app.sessions.services_container import SessionServices
from app.settings.service_config import SettingsConfigService
from app.settings.services_container import SettingsServices
from app.webhooks.dispatcher import WebhookDeliveryLoop


@dataclass(frozen=True, slots=True)
class AppServices:
    events: EventServices
    settings: SettingsServices
    agent_comm: AgentCommServices
    devices: DeviceServices
    hosts: HostServices
    packs: PackServices
    plugins: PluginServices
    sessions: SessionServices
    runs: RunServices
    grid: GridServices
    appium_nodes: AppiumNodeServices
    jobs: DurableJobWorkerLoop
    webhooks: WebhookDeliveryLoop
    background_loop_flush: BackgroundLoopFlushLoop
    leader_keepalive: LeaderKeepaliveLoop
    leader_watcher: LeaderWatcherLoop


def compose_app(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    bus: EventBus,
    settings_svc: SettingsService,
    http_pool: AgentHttpPool,
    circuit_breaker: AgentCircuitBreaker,
    control_plane_leader: ControlPlaneLeader,
) -> AppServices:
    """Wire the full dependency graph. Called once at startup."""
    event_services = EventServices(
        publisher=bus,
        subscriber=bus,
        reader=bus,
        session_factory=session_factory,
        engine=engine,
    )
    settings_services = SettingsServices(
        service=settings_svc,
        config=SettingsConfigService(publisher=bus),
        session_factory=session_factory,
    )
    agent_comm_services = AgentCommServices(
        http_pool=http_pool,
        circuit_breaker=circuit_breaker,
    )

    grid_svc = GridService(settings=settings_svc)

    pack_storage = PackStorageService(root=packs_settings.driver_pack_storage_dir)
    pack_feature = FeatureService(publisher=bus, circuit_breaker=circuit_breaker)
    pack_lifecycle = PackLifecycleService()
    pack_catalog = PackCatalogService(lifecycle=pack_lifecycle)
    pack_release = PackReleaseService(storage=pack_storage)
    pack_status = PackStatusService(feature=pack_feature)

    device_state_svc = DeviceStateService(publisher=bus)
    fleet_capacity_svc = FleetCapacityService(grid=grid_svc)
    data_cleanup_svc = DataCleanupService(publisher=bus, settings=settings_svc)

    run_release = RunReleaseService(publisher=bus, settings=settings_svc, grid=grid_svc, device_state=device_state_svc)
    run_lifecycle = RunLifecycleService(publisher=bus, settings=settings_svc, grid=grid_svc, release=run_release)
    run_allocator = RunAllocatorService(publisher=bus, settings=settings_svc, device_state=device_state_svc)
    run_failure = RunFailureService(publisher=bus, settings=settings_svc, circuit_breaker=circuit_breaker)
    run_query = RunQueryService()

    return AppServices(
        events=event_services,
        settings=settings_services,
        agent_comm=agent_comm_services,
        devices=DeviceServices(
            state=device_state_svc,
            fleet_capacity=fleet_capacity_svc,
            data_cleanup=data_cleanup_svc,
            publisher=bus,
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            circuit_breaker=circuit_breaker,
        ),
        hosts=HostServices(
            crud=HostCrudService(publisher=bus, settings=settings_svc),
            hardware_telemetry=HardwareTelemetryService(
                publisher=bus, settings=settings_svc, circuit_breaker=circuit_breaker
            ),
            resource_telemetry=HostResourceTelemetryService(settings=settings_svc, circuit_breaker=circuit_breaker),
            diagnostics=HostDiagnosticsService(circuit_breaker=circuit_breaker),
            publisher=bus,
            settings=settings_svc,
            pool=http_pool,
            circuit_breaker=circuit_breaker,
            session_factory=session_factory,
        ),
        sessions=SessionServices(
            crud=SessionCrudService(publisher=bus, device_state=device_state_svc),
            sync=SessionSyncService(publisher=bus, settings=settings_svc, grid=grid_svc),
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            publisher=bus,
        ),
        runs=RunServices(
            allocator=run_allocator,
            lifecycle=run_lifecycle,
            release=run_release,
            failure=run_failure,
            query=run_query,
            settings=settings_svc,
            session_factory=session_factory,
        ),
        grid=GridServices(
            grid=grid_svc,
            settings=settings_svc,
            session_factory=session_factory,
        ),
        packs=PackServices(
            catalog=pack_catalog,
            release=pack_release,
            status=pack_status,
            lifecycle=pack_lifecycle,
            feature=pack_feature,
            storage=pack_storage,
            publisher=bus,
            circuit_breaker=circuit_breaker,
            session_factory=session_factory,
        ),
        plugins=PluginServices(
            plugin=PluginService(settings=settings_svc, circuit_breaker=circuit_breaker),
            session_factory=session_factory,
        ),
        appium_nodes=AppiumNodeServices(
            reconciler=ReconcilerService(
                publisher=bus,
                settings=settings_svc,
                pool=http_pool,
                circuit_breaker=circuit_breaker,
                session_factory=session_factory,
            ),
            node_health=NodeHealthService(
                publisher=bus,
                settings=settings_svc,
                pool=http_pool,
                circuit_breaker=circuit_breaker,
                grid=grid_svc,
            ),
            heartbeat=HeartbeatService(
                publisher=bus,
                settings=settings_svc,
                pool=http_pool,
                circuit_breaker=circuit_breaker,
                session_factory=session_factory,
            ),
            settings=settings_svc,
            session_factory=session_factory,
        ),
        jobs=DurableJobWorkerLoop(
            service=DurableJobService(
                session_factory=session_factory,
                publisher=bus,
                settings=settings_svc,
                circuit_breaker=circuit_breaker,
            )
        ),
        webhooks=WebhookDeliveryLoop(session_factory=session_factory),
        background_loop_flush=BackgroundLoopFlushLoop(session_factory=session_factory, settings=settings_svc),
        leader_keepalive=LeaderKeepaliveLoop(settings=settings_svc),
        leader_watcher=LeaderWatcherLoop(settings=settings_svc, leader=control_plane_leader, engine=engine),
    )
