from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceAvailabilityStatus
from app.models.session import Session, SessionStatus
from app.services import session_service
from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_update_session_status_restores_busy_device_when_last_session_finishes(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="android-stale-busy",
        connection_target="android-stale-busy",
        name="Android stale-busy",
        os_version="14",
        availability_status="busy",
    )

    session = Session(session_id="android-sess-1", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "android-sess-1", SessionStatus.passed)

    assert updated is not None
    assert updated.status == SessionStatus.passed
    assert updated.ended_at is not None

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_update_session_status_preserves_busy_when_another_session_is_running(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="busy-multi-session",
        connection_target="busy-multi-session",
        name="Busy Multi Session",
        os_version="14",
        availability_status="busy",
    )

    db_session.add_all(
        [
            Session(session_id="sess-a", device_id=device.id, status=SessionStatus.running),
            Session(session_id="sess-b", device_id=device.id, status=SessionStatus.running),
        ]
    )
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "sess-a", SessionStatus.failed)

    assert updated is not None
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_update_session_status_restores_reserved_when_active_run_owns_device(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="reserved-session-device",
        connection_target="reserved-session-device",
        name="Reserved Session Device",
        os_version="14",
        availability_status="busy",
    )
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    await create_reserved_run(db_session, name="Reserved Session Run", devices=[device])

    session = Session(session_id="reserved-sess", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "reserved-sess", SessionStatus.error)

    assert updated is not None
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.reserved

    result = await db_session.execute(select(Session).where(Session.session_id == "reserved-sess"))
    stored = result.scalar_one()
    assert stored.ended_at is not None
