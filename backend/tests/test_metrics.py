from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import DEVICES_IN_COOLDOWN, refresh_system_gauges
from app.models.device_reservation import DeviceReservation
from app.models.test_run import RunState, TestRun
from tests.helpers import create_device_record


@pytest.mark.db
async def test_refresh_system_gauges_counts_active_cooldowns(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    now = datetime.now(UTC)
    active_device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="metrics-cooldown-active",
        connection_target="metrics-cooldown-active",
        name="Metrics Cooldown Active",
        availability_status="available",
    )
    expired_device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="metrics-cooldown-expired",
        connection_target="metrics-cooldown-expired",
        name="Metrics Cooldown Expired",
        availability_status="available",
    )
    run = TestRun(
        name="Metrics Cooldown Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 2}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add_all(
        [
            DeviceReservation(
                run=run,
                device_id=active_device.id,
                identity_value=active_device.identity_value,
                connection_target=active_device.connection_target,
                pack_id=active_device.pack_id,
                platform_id=active_device.platform_id,
                os_version=active_device.os_version,
                excluded=True,
                excluded_at=now,
                excluded_until=now + timedelta(seconds=60),
            ),
            DeviceReservation(
                run=run,
                device_id=expired_device.id,
                identity_value=expired_device.identity_value,
                connection_target=expired_device.connection_target,
                pack_id=expired_device.pack_id,
                platform_id=expired_device.platform_id,
                os_version=expired_device.os_version,
                excluded=True,
                excluded_at=now - timedelta(seconds=120),
                excluded_until=now - timedelta(seconds=60),
            ),
        ]
    )
    await db_session.commit()

    await refresh_system_gauges(db_session)

    assert DEVICES_IN_COOLDOWN._value.get() == 1
