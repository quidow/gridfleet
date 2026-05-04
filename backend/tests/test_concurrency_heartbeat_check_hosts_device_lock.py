import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import device_locking, heartbeat
from app.services.settings_service import settings_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_check_hosts_locks_device_rows_before_offline_write(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Offline host writes must serialize through the Device row lock."""

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="hb-offline-lock",
        availability_status=DeviceAvailabilityStatus.available,
    )
    device_id = device.id

    threshold = int(settings_service.get("general.max_missed_heartbeats"))

    inside_offline_branch = asyncio.Event()
    race_attempted_lock = asyncio.Event()
    race_committed = asyncio.Event()

    original_set_availability = heartbeat.set_device_availability_status

    async def gated_set_device_availability_status(
        device: Device,
        availability_status: DeviceAvailabilityStatus,
        *,
        reason: str | None = None,
        publish_event: bool = True,
    ) -> None:
        if device.id == device_id and availability_status == DeviceAvailabilityStatus.offline:
            inside_offline_branch.set()
            await asyncio.wait_for(race_attempted_lock.wait(), timeout=2.0)
            # Pre-fix, the racing writer can acquire and commit during this
            # gate because _check_hosts has not locked the Device row. Post-fix,
            # the racing writer blocks on lock_device until _check_hosts commits;
            # the timeout lets the fixed path continue without deadlocking.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(race_committed.wait(), timeout=0.5)
        await original_set_availability(
            device,
            availability_status,
            reason=reason,
            publish_event=publish_event,
        )

    async def heartbeat_caller() -> None:
        with (
            patch.object(heartbeat, "_ping_agent", new=AsyncMock(return_value=None)),
            patch.object(heartbeat, "set_device_availability_status", new=gated_set_device_availability_status),
        ):
            async with db_session_maker() as db:
                for _ in range(threshold):
                    await heartbeat._check_hosts(db)

    async def race_writer() -> None:
        await asyncio.wait_for(inside_offline_branch.wait(), timeout=2.0)
        async with db_session_maker() as db:
            race_attempted_lock.set()
            device = await device_locking.lock_device(db, device_id)
            device.availability_status = DeviceAvailabilityStatus.reserved
            await db.commit()
        race_committed.set()

    await asyncio.wait_for(asyncio.gather(heartbeat_caller(), race_writer()), timeout=5.0)

    async with db_session_maker() as db:
        final = (await db.execute(select(Device.availability_status).where(Device.id == device_id))).scalar_one()

    assert final == DeviceAvailabilityStatus.reserved, (
        f"Heartbeat _check_hosts did not lock device rows before the offline "
        f"write; final availability_status={final} indicates the host-offline "
        f"write raced the concurrent reservation writer instead of serializing "
        f"through device_locking.lock_devices"
    )
