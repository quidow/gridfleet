from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import Device, DeviceOperationalState
from app.devices.services.state import (
    DeviceStateFacts,
    derive_operational_state,
    evaluate_operational_state,
    gather_device_state_facts,
    is_available_sql,
    operational_state_sql,
)
from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

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

    now = datetime.now(UTC)
    assert await derive_operational_state(db_session, device, now=now) is DeviceOperationalState.available
    sql_state = (
        await db_session.execute(select(operational_state_sql(now=now)).where(Device.id == device.id))
    ).scalar_one()
    assert sql_state == DeviceOperationalState.available.value
    assert (
        await db_session.execute(select(Device.id).where(Device.id == device.id, is_available_sql(now=now)))
    ).scalar_one() == device.id


@pytest.mark.db
async def test_gather_facts_prefetched_packs_match_per_device(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A prefetched pack catalog (reconciler-loop batch path) must yield byte-identical
    facts to the per-device pack load, across ready and not-ready devices."""
    from app.devices.services.readiness import load_packs_by_ids

    await seed_test_packs(db_session)
    host = await create_host(client)
    ready = await create_device_record(
        db_session, host_id=host["id"], identity_value="eq-ready", name="ready", verified=True
    )
    unready = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="eq-unready",
        name="unready",
        platform_id="no_such_platform",
        verified=True,
    )
    now = datetime.now(UTC)
    catalog = await load_packs_by_ids(db_session, {ready.pack_id, unready.pack_id})

    for device in (ready, unready):
        baseline = await gather_device_state_facts(db_session, device, now=now)
        hinted = await gather_device_state_facts(db_session, device, now=now, packs=catalog)
        assert hinted == baseline

    # sanity: the two devices genuinely differ on readiness (otherwise the test is vacuous)
    assert (await gather_device_state_facts(db_session, ready, now=now)).ready is True
    assert (await gather_device_state_facts(db_session, unready, now=now)).ready is False


@pytest.mark.db
async def test_gather_facts_skip_reload_matches_reload_path(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """gather_device_state_facts must produce identical facts whether the passed device
    already has appium_node loaded (reload skipped — the reconciler path) or not (reload
    taken). Exercises stop_in_flight, which depends on the node row."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select

    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices import locking as device_locking
    from app.devices.models import Device

    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session, host_id=host["id"], identity_value="reload-eq", name="reload-eq", verified=True
    )
    node = AppiumNode(device_id=device.id, port=4723, desired_state=AppiumDesiredState.stopped)
    db_session.add(node)
    await db_session.commit()
    device_id = device.id
    now = datetime.now(UTC)

    # Path A: appium_node eager-loaded (reconciler path) -> reload skipped inside gather.
    loaded = await device_locking.lock_device(db_session, device_id)
    assert "appium_node" not in sa_inspect(loaded).unloaded
    facts_loaded = await gather_device_state_facts(db_session, loaded, now=now)

    # Path B: appium_node NOT loaded -> reload taken inside gather.
    db_session.expire_all()
    unloaded = (await db_session.execute(select(Device).where(Device.id == device_id))).scalar_one()
    assert "appium_node" in sa_inspect(unloaded).unloaded
    facts_unloaded = await gather_device_state_facts(db_session, unloaded, now=now)

    assert facts_loaded == facts_unloaded
    assert facts_loaded.stop_in_flight is True  # non-trivial: derived from the node row


@pytest.mark.db
async def test_gather_facts_maintenance_masks_not_ready(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The ready fold consumes the whole WithdrawalFacts group, so a maintenance
    device gathers ready=False — and the evaluator's masking order still derives
    maintenance, never offline, exactly as before the fold."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="facts-maint-01",
        name="Facts Maintenance Device",
        verified=True,
        lifecycle_policy_state={"maintenance_reason": "operator"},
    )

    facts = await gather_device_state_facts(db_session, device, now=datetime.now(UTC))

    assert facts.in_maintenance is True
    # in_service() folds ¬in_maintenance into ready; before the fold this was True.
    assert facts.ready is False
    assert evaluate_operational_state(facts) is DeviceOperationalState.maintenance
