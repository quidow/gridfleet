from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.device_reservation import DeviceReservation
from app.models.test_run import RunState, TestRun
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_device_reservations_have_excluded_window_range(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'device_reservations'
              AND column_name = 'excluded_window'
            """
        )
    )

    assert result.one() == ("tstzrange", "tstzrange")


@pytest.mark.db
@pytest.mark.asyncio
async def test_excluded_windows_cannot_overlap_for_same_device(db_session: AsyncSession, default_host_id: str) -> None:
    device = await create_device(db_session, host_id=uuid.UUID(default_host_id), name="range device")
    run = TestRun(
        name="range run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()
    now = datetime.now(UTC)

    first = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        excluded=True,
        excluded_at=now,
        excluded_until=now + timedelta(minutes=10),
        released_at=now + timedelta(minutes=11),
    )
    second = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        excluded=True,
        excluded_at=now + timedelta(minutes=5),
        excluded_until=now + timedelta(minutes=15),
        released_at=now + timedelta(minutes=16),
    )
    db_session.add_all([first, second])

    with pytest.raises(IntegrityError) as exc_info:
        await db_session.flush()
    assert "ex_device_reservations_device_excluded_window" in str(exc_info.value)
