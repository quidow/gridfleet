"""Host domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.hosts.protocols import HostCrudProtocol
    from app.hosts.service_agent_logs import AgentLogsService
    from app.hosts.service_diagnostics import HostDiagnosticsService
    from app.hosts.service_hardware_telemetry import HardwareTelemetryService
    from app.hosts.service_host_events import HostEventsService
    from app.hosts.service_resource_telemetry import HostResourceTelemetryService


@dataclass(frozen=True, slots=True)
class HostServices:
    crud: HostCrudProtocol
    hardware_telemetry: HardwareTelemetryService
    resource_telemetry: HostResourceTelemetryService
    diagnostics: HostDiagnosticsService
    agent_logs: AgentLogsService
    host_events: HostEventsService
    publisher: EventPublisher
    settings: SettingsReader
    pool: AgentHttpPool
    circuit_breaker: CircuitBreakerProtocol
    session_factory: async_sessionmaker[AsyncSession]
