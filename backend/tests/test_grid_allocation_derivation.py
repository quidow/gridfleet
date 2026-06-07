"""A pending session (allocation claimed, Appium session not yet confirmed) derives ``busy``.

``pending`` is "a session is being created" — the device must leave the
allocatable pool immediately, exactly like ``running``.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceOperationalState
from app.devices.services.state import apply_derived_state
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record, create_host
from tests.helpers import test_event_bus as event_bus
from tests.pack.factories import seed_test_packs


@pytest.mark.db
async def test_pending_session_derives_busy(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="grid-alloc-pending-01",
        name="GridAllocPending",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    db_session.add(
        Session(
            id=uuid.uuid4(),
            session_id=f"alloc-{uuid.uuid4()}",
            device_id=device.id,
            status=SessionStatus.pending,
        )
    )
    await db_session.flush()

    await apply_derived_state(db_session, device, now=datetime.now(UTC), publisher=event_bus)

    assert device.operational_state is DeviceOperationalState.busy
