"""Device domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.devices.protocols import (
        DeviceCapabilityProtocol,
        DeviceCrudProtocol,
        DeviceHealthProtocol,
        MaintenanceProtocol,
    )
    from app.devices.services.bulk import BulkOperationsService
    from app.devices.services.connectivity import ConnectivityService
    from app.devices.services.data_cleanup import DataCleanupService
    from app.devices.services.fleet_capacity import FleetCapacityService
    from app.devices.services.groups import DeviceGroupsService
    from app.devices.services.presenter import DevicePresenterService
    from app.devices.services.property_refresh import PropertyRefreshService
    from app.devices.services.test_data import TestDataService
    from app.events.protocols import EventPublisher


@dataclass(frozen=True, slots=True)
class DeviceServices:
    fleet_capacity: FleetCapacityService
    data_cleanup: DataCleanupService
    property_refresh: PropertyRefreshService
    groups: DeviceGroupsService
    maintenance: MaintenanceProtocol
    bulk: BulkOperationsService
    presenter: DevicePresenterService
    test_data: TestDataService
    crud: DeviceCrudProtocol
    capability: DeviceCapabilityProtocol
    connectivity: ConnectivityService
    health: DeviceHealthProtocol
    publisher: EventPublisher
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
    circuit_breaker: CircuitBreakerProtocol
    # Carries the agent BasicAuth credentials for backend→agent calls (reconfigure
    # delivery). None means unauthenticated, which only works when the auth gate is off.
    pool: AgentHttpPool | None = None
