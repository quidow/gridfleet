from __future__ import annotations

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason
from app.devices.services.allocatability import is_allocatable, unavailable_reason


def test_available_and_free_is_allocatable() -> None:
    assert is_allocatable(DeviceOperationalState.available, is_reserved=False) is True
    assert unavailable_reason(DeviceOperationalState.available, is_reserved=False) is None


def test_available_but_reserved_reports_reserved() -> None:
    assert is_allocatable(DeviceOperationalState.available, is_reserved=True) is False
    assert unavailable_reason(DeviceOperationalState.available, is_reserved=True) is UnavailableReason.reserved


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DeviceOperationalState.busy, UnavailableReason.busy),
        (DeviceOperationalState.verifying, UnavailableReason.verifying),
        (DeviceOperationalState.maintenance, UnavailableReason.maintenance),
        (DeviceOperationalState.offline, UnavailableReason.offline),
    ],
)
def test_operational_state_dominates_over_reservation(
    state: DeviceOperationalState, expected: UnavailableReason
) -> None:
    # A non-available operational cause is reported regardless of reservation.
    assert unavailable_reason(state, is_reserved=False) is expected
    assert unavailable_reason(state, is_reserved=True) is expected
    assert is_allocatable(state, is_reserved=False) is False
