"""apply_derived_state persists the typed DeviceEvent audit row on a transition — but
only when the observation site carries the cause (§6: the cause rides on the observation).

The reconciler must NOT guess the cause from facts: a not-ready offline transition could be a
connectivity loss, a node crash, a health-probe failure or a verification failure, each with a
different DeviceEventType. Guessing would corrupt the analytics reliability counts that query
``connectivity_lost``. So a carried ``observed_reason`` produces a row; an uncarried reconcile
updates state + emits the bus event but records no row.

"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import DeviceEvent, DeviceEventType, DeviceOperationalState
from app.devices.services.observation_reason import ObservationReason
from app.devices.services.state import apply_derived_state
from tests.helpers import create_device_record, create_host
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


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
        db_session, device, now=datetime.now(UTC), observed_reason=ObservationReason.disconnected, publisher=event_bus
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

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC), publisher=event_bus)

    assert changed is True
    assert device.operational_state is DeviceOperationalState.offline
    assert await _event_types(db_session, device.id) == []
