from app.devices.protocols import FleetCapacityProtocol
from app.devices.services.fleet_capacity import FleetCapacityService


def test_fleet_capacity_service_satisfies_protocol() -> None:
    assert isinstance(FleetCapacityService.__new__(FleetCapacityService), FleetCapacityProtocol)
