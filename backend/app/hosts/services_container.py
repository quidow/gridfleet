"""Host domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.hosts.service import HostCrudService
    from app.hosts.service_diagnostics import HostDiagnosticsService
    from app.hosts.service_hardware_telemetry import HardwareTelemetryService
    from app.hosts.service_host_events import HostEventsService
    from app.hosts.service_resource_telemetry import HostResourceTelemetryService


@dataclass(frozen=True, slots=True)
class HostServices:
    crud: HostCrudService
    hardware_telemetry: HardwareTelemetryService
    resource_telemetry: HostResourceTelemetryService
    diagnostics: HostDiagnosticsService
    host_events: HostEventsService
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
