import pytest

from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services.state_derivation import (
    DeviceStateFacts,
    evaluate_hold,
    evaluate_operational_state,
)

_ALL_FALSE = dict(
    has_running_session=False,
    has_verification_lease=False,
    in_maintenance=False,
    stop_in_flight=False,
    ready=True,
    is_reserved=False,
)


def _facts(**overrides: bool) -> DeviceStateFacts:
    return DeviceStateFacts(**{**_ALL_FALSE, **overrides})


@pytest.mark.parametrize(
    "facts,expected",
    [
        (_facts(), DeviceOperationalState.available),
        (_facts(has_running_session=True), DeviceOperationalState.busy),
        (_facts(has_verification_lease=True), DeviceOperationalState.verifying),
        (_facts(stop_in_flight=True), DeviceOperationalState.offline),
        (_facts(ready=False), DeviceOperationalState.offline),
        # precedence: session beats verification
        (_facts(has_running_session=True, has_verification_lease=True), DeviceOperationalState.busy),
        # precedence: verification beats offline
        (_facts(has_verification_lease=True, stop_in_flight=True), DeviceOperationalState.verifying),
        # maintenance/reservation never affect the operational axis in Stage 1
        (_facts(in_maintenance=True), DeviceOperationalState.available),
        (_facts(is_reserved=True), DeviceOperationalState.available),
    ],
)
def test_evaluate_operational_state(facts: DeviceStateFacts, expected: DeviceOperationalState) -> None:
    assert evaluate_operational_state(facts) is expected


@pytest.mark.parametrize(
    "facts,expected",
    [
        (_facts(), None),
        (_facts(in_maintenance=True), DeviceHold.maintenance),
        (_facts(is_reserved=True), DeviceHold.reserved),
        # maintenance outranks reserved
        (_facts(in_maintenance=True, is_reserved=True), DeviceHold.maintenance),
    ],
)
def test_evaluate_hold(facts: DeviceStateFacts, expected: DeviceHold | None) -> None:
    assert evaluate_hold(facts) is expected
