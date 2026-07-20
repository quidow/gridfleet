from __future__ import annotations

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import UnavailableReason
from app.devices.services.allocatability import unavailable_reason


def test_available_and_free_is_allocatable() -> None:
    assert (
        unavailable_reason(
            DeviceOperationalState.available, reserved=False, accepting_new_sessions=True, node_viable=True
        )
        is None
    )


def test_available_but_reserved_reports_reserved() -> None:
    assert (
        unavailable_reason(
            DeviceOperationalState.available, reserved=True, accepting_new_sessions=True, node_viable=True
        )
        is UnavailableReason.reserved
    )


def test_available_not_accepting_reports_cooldown() -> None:
    # Warm soft-gate (Stage 2): an available device whose node stopped accepting
    # new sessions is parked (cooldown is the only Stage-2 producer), even though
    # it is free and viable. Gate-honest with allocation._eligible_devices_with_facts.
    assert (
        unavailable_reason(
            DeviceOperationalState.available, reserved=False, accepting_new_sessions=False, node_viable=True
        )
        is UnavailableReason.cooldown
    )


def test_available_but_node_not_viable_reports_transitioning() -> None:
    # Stage 4 / P6: the node is healthy (device still ``available``) but mid-transition
    # — a restart is in flight or the routable target is not yet settled — so the
    # allocator's node-viability gate refuses it. Surfaced WITHOUT an operational_state flip.
    assert (
        unavailable_reason(
            DeviceOperationalState.available, reserved=False, accepting_new_sessions=True, node_viable=False
        )
        is UnavailableReason.transitioning
    )


def test_node_viability_outranks_cooldown_and_reservation() -> None:
    # Mirror allocation._claim's lock-time recheck order: viability is checked before the
    # warm soft-gate and the reservation gate, so a not-viable node reports transitioning
    # even when it is also soft-gated and reserved.
    assert (
        unavailable_reason(
            DeviceOperationalState.available, reserved=True, accepting_new_sessions=False, node_viable=False
        )
        is UnavailableReason.transitioning
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
def test_operational_state_dominates_over_reservation_and_viability(
    state: DeviceOperationalState, expected: UnavailableReason
) -> None:
    # A non-available operational cause is reported regardless of reservation or viability.
    assert unavailable_reason(state, reserved=False, accepting_new_sessions=True, node_viable=True) is expected
    assert unavailable_reason(state, reserved=True, accepting_new_sessions=True, node_viable=False) is expected


def test_every_operational_state_is_handled() -> None:
    # Runtime companion to the match/assert_never exhaustiveness guard: every state
    # maps, and `available` (when free, accepting, and viable) is the only allocatable outcome.
    for state in DeviceOperationalState:
        reason = unavailable_reason(state, reserved=False, accepting_new_sessions=True, node_viable=True)
        if state is DeviceOperationalState.available:
            assert reason is None
        else:
            assert reason is not None
