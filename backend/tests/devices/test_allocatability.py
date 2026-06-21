from __future__ import annotations

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason
from app.devices.services.allocatability import unavailable_reason


def test_available_and_free_is_allocatable() -> None:
    assert unavailable_reason(DeviceOperationalState.available, reserved=False, accepting_new_sessions=True) is None


def test_available_but_reserved_reports_reserved() -> None:
    assert (
        unavailable_reason(DeviceOperationalState.available, reserved=True, accepting_new_sessions=True)
        is UnavailableReason.reserved
    )


def test_available_not_accepting_reports_cooldown() -> None:
    # Warm soft-gate (Stage 2): an available device whose node stopped accepting
    # new sessions is parked (cooldown is the only Stage-2 producer), even though
    # it is free. Gate-honest with allocation._eligible_devices.
    assert (
        unavailable_reason(DeviceOperationalState.available, reserved=False, accepting_new_sessions=False)
        is UnavailableReason.cooldown
    )


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
    assert unavailable_reason(state, reserved=False, accepting_new_sessions=True) is expected
    assert unavailable_reason(state, reserved=True, accepting_new_sessions=True) is expected


def test_every_operational_state_is_handled() -> None:
    # Runtime companion to the match/assert_never exhaustiveness guard: every state
    # maps, and `available` (when free) is the only allocatable outcome.
    for state in DeviceOperationalState:
        reason = unavailable_reason(state, reserved=False, accepting_new_sessions=True)
        if state is DeviceOperationalState.available:
            assert reason is None
        else:
            assert reason is not None
