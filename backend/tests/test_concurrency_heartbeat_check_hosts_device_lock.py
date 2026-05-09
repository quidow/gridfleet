import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.services import device_locking, heartbeat
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
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
        operational_state=DeviceOperationalState.available,
    )
    device_id = device.id

    threshold = int(settings_service.get("general.max_missed_heartbeats"))

    inside_offline_branch = asyncio.Event()
    race_attempted_lock = asyncio.Event()
    race_committed = asyncio.Event()

    original_set_operational_state = heartbeat.set_operational_state

    async def gated_set_operational_state(
        device: Device,
        operational_state: DeviceOperationalState,
        *,
        reason: str | None = None,
        publish_event: bool = True,
    ) -> None:
        if device.id == device_id and operational_state == DeviceOperationalState.offline:
            inside_offline_branch.set()
            await asyncio.wait_for(race_attempted_lock.wait(), timeout=2.0)
            # Pre-fix, the racing writer can acquire and commit during this
            # gate because _check_hosts has not locked the Device row. Post-fix,
            # the racing writer blocks on lock_device until _check_hosts commits;
            # the timeout lets the fixed path continue without deadlocking.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(race_committed.wait(), timeout=0.5)
        await original_set_operational_state(
            device,
            operational_state,
            reason=reason,
            publish_event=publish_event,
        )

    _dead_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.connect_error,
        payload=None,
        duration_ms=0,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category=None,
    )

    async def heartbeat_caller() -> None:
        with (
            patch.object(heartbeat, "_ping_agent", new=AsyncMock(return_value=_dead_result)),
            patch.object(heartbeat, "set_operational_state", new=gated_set_operational_state),
            patch.object(heartbeat, "assert_current_leader", new=AsyncMock()),
            patch.object(heartbeat, "async_session", db_session_maker),
        ):
            async with db_session_maker() as db:
                for _ in range(threshold):
                    await heartbeat._check_hosts(db)

    async def race_writer() -> None:
        await asyncio.wait_for(inside_offline_branch.wait(), timeout=2.0)
        async with db_session_maker() as db:
            race_attempted_lock.set()
            device = await device_locking.lock_device(db, device_id)
            device.hold = DeviceHold.reserved
            await db.commit()
        race_committed.set()

    await asyncio.wait_for(asyncio.gather(heartbeat_caller(), race_writer()), timeout=5.0)

    async with db_session_maker() as db:
        final = (await db.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))).one()

    assert final.operational_state == DeviceOperationalState.offline
    assert final.hold == DeviceHold.reserved, (
        f"Heartbeat _check_hosts did not lock device rows before the offline "
        f"write; final hold={final.hold} indicates the host-offline "
        f"write raced the concurrent reservation writer instead of serializing "
        f"through device_locking.lock_devices"
    )
