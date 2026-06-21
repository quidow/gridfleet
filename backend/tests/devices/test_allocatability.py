from __future__ import annotations

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason
from app.devices.services.allocatability import unavailable_reason


def test_available_and_free_is_allocatable() -> None:
    assert unavailable_reason(DeviceOperationalState.available, reserved=False) is None


def test_available_but_reserved_reports_reserved() -> None:
    assert unavailable_reason(DeviceOperationalState.available, reserved=True) is UnavailableReason.reserved


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
    assert unavailable_reason(state, reserved=False) is expected
    assert unavailable_reason(state, reserved=True) is expected


def test_every_operational_state_is_handled() -> None:
    # Runtime companion to the match/assert_never exhaustiveness guard: every state
    # maps, and `available` (when free) is the only allocatable outcome.
    for state in DeviceOperationalState:
        reason = unavailable_reason(state, reserved=False)
        if state is DeviceOperationalState.available:
            assert reason is None
        else:
            assert reason is not None
