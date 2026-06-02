"""Device domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.devices.protocols import (
        BulkOperationsProtocol,
        ConnectivityProtocol,
        DataCleanupProtocol,
        DeviceCapabilityProtocol,
        DeviceCrudProtocol,
        DeviceGroupsProtocol,
        DeviceHealthProtocol,
        DevicePresenterProtocol,
        FleetCapacityProtocol,
        MaintenanceProtocol,
        PropertyRefreshProtocol,
        TestDataProtocol,
    )
    from app.events.protocols import EventPublisher
    from app.grid.protocols import GridServiceProtocol


@dataclass(frozen=True, slots=True)
class DeviceServices:
    fleet_capacity: FleetCapacityProtocol
    data_cleanup: DataCleanupProtocol
    property_refresh: PropertyRefreshProtocol
    groups: DeviceGroupsProtocol
    maintenance: MaintenanceProtocol
    bulk: BulkOperationsProtocol
    presenter: DevicePresenterProtocol
    test_data: TestDataProtocol
    crud: DeviceCrudProtocol
    capability: DeviceCapabilityProtocol
    connectivity: ConnectivityProtocol
    health: DeviceHealthProtocol
    publisher: EventPublisher
    settings: SettingsReader
    grid: GridServiceProtocol
    session_factory: async_sessionmaker[AsyncSession]
    circuit_breaker: CircuitBreakerProtocol
