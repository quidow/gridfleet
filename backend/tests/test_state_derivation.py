from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceOperationalState
from app.devices.services.state_derivation import (
    DeviceStateFacts,
    evaluate_operational_state,
    gather_device_state_facts,
)
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs

_BASELINE = dict(
    has_running_session=False,
    has_verification_lease=False,
    in_maintenance=False,
    stop_in_flight=False,
    ready=True,
)


def _facts(**overrides: bool) -> DeviceStateFacts:
    return DeviceStateFacts(**{**_BASELINE, **overrides})


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
        # §4: maintenance derives onto the operational axis when idle
        (_facts(in_maintenance=True), DeviceOperationalState.maintenance),
        # busy/verifying outrank maintenance
        (_facts(in_maintenance=True, has_running_session=True), DeviceOperationalState.busy),
        (_facts(in_maintenance=True, has_verification_lease=True), DeviceOperationalState.verifying),
        # maintenance outranks offline: a maintenance device whose node is down stays maintenance
        (_facts(in_maintenance=True, stop_in_flight=True, ready=False), DeviceOperationalState.maintenance),
    ],
)
def test_evaluate_operational_state(facts: DeviceStateFacts, expected: DeviceOperationalState) -> None:
    assert evaluate_operational_state(facts) is expected


@pytest.mark.db
async def test_gather_facts_available_device(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """gather_device_state_facts returns the right fact-bag for a healthy, available device."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="facts-avail-01",
        name="Facts Available Device",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )

    facts = await gather_device_state_facts(db_session, device, now=datetime.now(UTC))

    assert facts.has_running_session is False
    assert facts.has_verification_lease is False
    assert facts.in_maintenance is False
    assert facts.stop_in_flight is False
    assert facts.ready is True
    assert evaluate_operational_state(facts) is DeviceOperationalState.available
