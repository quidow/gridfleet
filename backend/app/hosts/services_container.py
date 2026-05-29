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
    from app.hosts.protocols import (
        HardwareTelemetryProtocol,
        HostCrudProtocol,
        HostDiagnosticsProtocol,
        HostResourceTelemetryProtocol,
    )


@dataclass(frozen=True, slots=True)
class HostServices:
    crud: HostCrudProtocol
    hardware_telemetry: HardwareTelemetryProtocol
    resource_telemetry: HostResourceTelemetryProtocol
    diagnostics: HostDiagnosticsProtocol
    publisher: EventPublisher
    settings: SettingsReader
    pool: AgentHttpPool
    circuit_breaker: CircuitBreakerProtocol
    session_factory: async_sessionmaker[AsyncSession]
