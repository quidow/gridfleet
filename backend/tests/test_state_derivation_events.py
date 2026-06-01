"""apply_derived_state persists the typed DeviceEvent audit row on a transition — but
only when the observation site carries the cause (§6: the cause rides on the observation).

The reconciler must NOT guess the cause from facts: a not-ready offline transition could be a
connectivity loss, a node crash, a health-probe failure or a verification failure, each with a
different DeviceEventType. Guessing would corrupt the analytics reliability counts that query
``connectivity_lost``. So a carried ``observed_reason`` produces a row; an uncarried reconcile
updates state + emits the bus event but records no row.

Maintenance hold transitions are structurally unambiguous (hold==maintenance has exactly one
cause) and always record their audit row.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceEvent, DeviceEventType, DeviceHold, DeviceOperationalState
from app.devices.services.observation_reason import ObservationReason
from app.devices.services.state_derivation import apply_derived_state
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs


async def _event_types(db: AsyncSession, device_id: object) -> list[DeviceEventType]:
    rows = (await db.execute(select(DeviceEvent).where(DeviceEvent.device_id == device_id))).scalars().all()
    return [r.event_type for r in rows]


@pytest.mark.db
async def test_carried_reason_writes_connectivity_lost_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """available → offline with observed_reason=disconnected records a connectivity_lost row.

    This is the analytics-critical path: analytics/service.py counts connectivity_lost.
    """
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="evt-offline-01",
        name="Offline",
        operational_state=DeviceOperationalState.available,
        verified=True,
        device_checks_healthy=False,
    )

    changed = await apply_derived_state(
        db_session, device, now=datetime.now(UTC), observed_reason=ObservationReason.disconnected
    )

    assert changed is True
    assert device.operational_state is DeviceOperationalState.offline
    assert DeviceEventType.connectivity_lost in await _event_types(db_session, device.id)


@pytest.mark.db
async def test_no_carried_reason_writes_no_operational_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """Without observed_reason the transition still derives + flips state but records no audit row.

    Prevents the reconciler from mislabelling fact-derived offline transitions (e.g. a node crash)
    as connectivity_lost, which would inflate analytics reliability counts.
    """
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="evt-offline-02",
        name="OfflineNoReason",
        operational_state=DeviceOperationalState.available,
        verified=True,
        device_checks_healthy=False,
    )

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC))

    assert changed is True
    assert device.operational_state is DeviceOperationalState.offline
    assert await _event_types(db_session, device.id) == []


@pytest.mark.db
async def test_enter_maintenance_writes_maintenance_entered_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """hold → maintenance records a maintenance_entered row (structural, no carried reason needed)."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="evt-maint-enter-01",
        name="MaintEnter",
        operational_state=DeviceOperationalState.available,
        verified=True,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC))

    assert changed is True
    assert device.hold is DeviceHold.maintenance
    assert DeviceEventType.maintenance_entered in await _event_types(db_session, device.id)


@pytest.mark.db
async def test_exit_maintenance_writes_maintenance_exited_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """maintenance → (none) records a maintenance_exited row."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    # Persisted hold=maintenance but no maintenance_reason signal → derives hold=None (exit).
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="evt-maint-exit-01",
        name="MaintExit",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.maintenance,
        verified=True,
    )

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC))

    assert changed is True
    assert device.hold is None
    assert DeviceEventType.maintenance_exited in await _event_types(db_session, device.id)
