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
    from app.events.event_bus import EventBus
    from app.settings.service import SettingsService

from app.agent_comm.services_container import AgentCommServices
from app.appium_nodes.services_container import AppiumNodeServices
from app.devices.services_container import DeviceServices
from app.events.services_container import EventServices
from app.grid.services_container import GridServices
from app.hosts.services_container import HostServices
from app.packs.services_container import PackServices
from app.runs.services_container import RunServices
from app.sessions.services_container import SessionServices
from app.settings.services_container import SettingsServices


@dataclass(frozen=True, slots=True)
class AppServices:
    events: EventServices
    settings: SettingsServices
    agent_comm: AgentCommServices
    devices: DeviceServices
    hosts: HostServices
    packs: PackServices
    sessions: SessionServices
    runs: RunServices
    grid: GridServices
    appium_nodes: AppiumNodeServices


def compose_app(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    bus: EventBus,
    settings_svc: SettingsService,
    http_pool: AgentHttpPool,
    circuit_breaker: AgentCircuitBreaker,
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
        reader=settings_svc,
        service=settings_svc,
        session_factory=session_factory,
    )
    agent_comm_services = AgentCommServices(
        http_pool=http_pool,
        circuit_breaker=circuit_breaker,
    )

    return AppServices(
        events=event_services,
        settings=settings_services,
        agent_comm=agent_comm_services,
        devices=DeviceServices(
            publisher=bus,
            settings=settings_svc,
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
        sessions=SessionServices(settings=settings_svc, session_factory=session_factory),
        runs=RunServices(publisher=bus, settings=settings_svc, session_factory=session_factory),
        grid=GridServices(settings=settings_svc, session_factory=session_factory),
        packs=PackServices(session_factory=session_factory),
        appium_nodes=AppiumNodeServices(
            settings=settings_svc,
            pool=http_pool,
            circuit_breaker=circuit_breaker,
            publisher=bus,
            session_factory=session_factory,
        ),
    )
