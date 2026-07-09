"""apply_derived_state records NO DeviceEvent audit rows — transitions are uncaused (plan 4b).

Causes are recorded once, at the observation sites that know them: the connectivity sweep and
host heartbeat loss record connectivity_lost, lifecycle escalation records node_crash /
health_check_fail, the maintenance service records maintenance_entered / maintenance_exited.
The reconciler only flips the axis and emits the device.operational_state_changed bus event.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import DeviceEvent, DeviceEventType, DeviceOperationalState
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
async def test_offline_transition_records_no_audit_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """available → offline flips the axis and emits the bus event but records no DeviceEvent.

    The cause (connectivity loss vs node crash vs health failure) is recorded by the
    observation site that saw it, not by the reconciler.
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

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC), publisher=event_bus)

    assert changed is True
    assert device.operational_state is DeviceOperationalState.offline
    assert await _event_types(db_session, device.id) == []


@pytest.mark.db
async def test_maintenance_transition_records_no_audit_row(client: AsyncClient, db_session: AsyncSession) -> None:
    """available → maintenance flips the axis but records nothing — the maintenance service is
    the sole writer of maintenance_entered/exited rows (at fact-write time)."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="evt-maint-01",
        name="MaintEnter",
        operational_state=DeviceOperationalState.available,
        verified=True,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )

    changed = await apply_derived_state(db_session, device, now=datetime.now(UTC), publisher=event_bus)

    assert changed is True
    assert device.operational_state is DeviceOperationalState.maintenance
    assert await _event_types(db_session, device.id) == []
