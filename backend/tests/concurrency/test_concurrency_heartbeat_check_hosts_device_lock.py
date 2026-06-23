import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.appium_nodes.services import heartbeat as heartbeat
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state as device_state
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

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

    # The host-offline write now derives through update_device_checks -> reconcile ->
    # apply_derived_state, which calls set_operational_state in app.devices.services.state
    # (heartbeat no longer calls it directly). The row lock is still acquired by
    # _apply_host_ping_result's lock_devices before that derivation, so gate on the
    # state-module call to prove the lock window covers the offline write.
    original_set_operational_state = device_state.set_operational_state

    async def gated_set_operational_state(
        device: Device,
        operational_state: DeviceOperationalState,
        *,
        reason: str | None = None,
        publish_event: bool = True,
        severity: str | None = None,
        publisher: object = None,
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
            severity=severity,
            publisher=publisher,
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
            patch.object(device_state, "set_operational_state", new=gated_set_operational_state),
            patch.object(heartbeat, "assert_current_leader", new=AsyncMock()),
        ):
            async with db_session_maker() as db:
                svc = HeartbeatService(
                    publisher=Mock(),
                    settings=FakeSettingsReader({}),
                    pool=Mock(),
                    circuit_breaker=Mock(),
                    session_factory=db_session_maker,
                )
                for _ in range(threshold):
                    await svc._check_hosts(db)

    async def race_writer() -> None:
        await asyncio.wait_for(inside_offline_branch.wait(), timeout=2.0)
        async with db_session_maker() as db:
            race_attempted_lock.set()
            device = await device_locking.lock_device(db, device_id)
            # Write an unguarded column under the row lock to prove the offline
            # write serialized through device_locking.lock_device before this
            # writer acquired the lock.
            device.model = "race-marker"
            await db.commit()
        race_committed.set()

    await asyncio.wait_for(asyncio.gather(heartbeat_caller(), race_writer()), timeout=5.0)

    async with db_session_maker() as db:
        final = (await db.execute(select(Device.operational_state, Device.model).where(Device.id == device_id))).one()

    assert final.operational_state == DeviceOperationalState.offline
    assert final.model == "race-marker", (
        f"Heartbeat _check_hosts did not lock device rows before the offline "
        f"write; final model={final.model} indicates the host-offline "
        f"write raced the concurrent writer instead of serializing "
        f"through device_locking.lock_devices"
    )
