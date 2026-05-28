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
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.leader.keepalive import LeaderKeepaliveLoop
from app.core.leader.watcher import LeaderWatcherLoop
from app.core.observability import BackgroundLoopFlushLoop
from app.devices.services_container import DeviceServices
from app.events.services_container import EventServices
from app.grid.service import GridService
from app.grid.services_container import GridServices
from app.hosts.services_container import HostServices
from app.jobs.queue import DurableJobService, DurableJobWorkerLoop
from app.packs.services_container import PackServices
from app.plugins.service import PluginService
from app.plugins.services_container import PluginServices
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

    return AppServices(
        events=event_services,
        settings=settings_services,
        agent_comm=agent_comm_services,
        devices=DeviceServices(
            publisher=bus,
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            circuit_breaker=circuit_breaker,
        ),
        hosts=HostServices(
            publisher=bus,
            settings=settings_svc,
            pool=http_pool,
            circuit_breaker=circuit_breaker,
            session_factory=session_factory,
        ),
        sessions=SessionServices(
            crud=SessionCrudService(publisher=bus),
            sync=SessionSyncService(publisher=bus, settings=settings_svc, grid=grid_svc),
            settings=settings_svc,
            grid=grid_svc,
            session_factory=session_factory,
            publisher=bus,
        ),
        runs=RunServices(publisher=bus, settings=settings_svc, grid=grid_svc, session_factory=session_factory),
        grid=GridServices(
            grid=grid_svc,
            settings=settings_svc,
            session_factory=session_factory,
        ),
        packs=PackServices(session_factory=session_factory),
        plugins=PluginServices(
            plugin=PluginService(settings=settings_svc, circuit_breaker=circuit_breaker),
            session_factory=session_factory,
        ),
        appium_nodes=AppiumNodeServices(
            settings=settings_svc,
            pool=http_pool,
            circuit_breaker=circuit_breaker,
            publisher=bus,
            grid=grid_svc,
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
