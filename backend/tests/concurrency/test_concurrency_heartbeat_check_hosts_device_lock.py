from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

from app.appium_nodes.services.heartbeat import HeartbeatService
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import intent_reconciler
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, run_one_heartbeat_cycle

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.hosts.models import Host
    from app.packs.models import DriverPack

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_host_sweep_locks_device_rows_before_offline_write(
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
    # Offline now derives from status-push recency: no push within the offline window.
    db_host.last_heartbeat = now_utc() - timedelta(minutes=10)
    await db_session.commit()

    # >= 2 iterations forces the offline flip on cycle 2 (cycle 1 is guarded).
    threshold = 2

    inside_offline_branch = asyncio.Event()
    race_attempted_lock = asyncio.Event()
    race_committed = asyncio.Event()

    # The host-offline write now derives through update_device_checks -> reconcile ->
    # the edge detector in intent_reconciler. Reconcile loads the decision snapshot
    # (async, right after lock_device_handle) before applying the synchronous
    # operational-state edge, all inside evaluate_host's lock_devices window. Gate on
    # that async loader to prove the lock window covers the offline write (the sync
    # apply_operational_state_transition cannot be patched with an async gate).
    original_loader = intent_reconciler.load_device_decision_snapshot

    async def gated_loader(
        db: AsyncSession,
        locked: LockedDevice,
        *,
        packs: Mapping[str, DriverPack],
        now: datetime,
    ) -> DeviceDecisionSnapshot:
        if locked.device.id == device_id:
            inside_offline_branch.set()
            await asyncio.wait_for(race_attempted_lock.wait(), timeout=2.0)
            # Pre-fix, the racing writer can acquire and commit during this
            # gate because the host sweep has not locked the Device row. Post-fix,
            # the racing writer blocks on lock_device until the sweep commits;
            # the timeout lets the fixed path continue without deadlocking.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(race_committed.wait(), timeout=0.5)
        return await original_loader(db, locked, packs=packs, now=now)

    async def heartbeat_caller() -> None:
        with patch.object(intent_reconciler, "load_device_decision_snapshot", new=gated_loader):
            async with db_session_maker() as db:
                svc = HeartbeatService(
                    publisher=Mock(),
                    settings=FakeSettingsReader({}),
                    pool=Mock(),
                    circuit_breaker=Mock(),
                    session_factory=db_session_maker,
                )
                for _ in range(threshold):
                    await run_one_heartbeat_cycle(db, svc)

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
        final = (
            await db.execute(select(Device.operational_state_last_emitted, Device.model).where(Device.id == device_id))
        ).one()

    assert final.operational_state_last_emitted == DeviceOperationalState.offline
    assert final.model == "race-marker", (
        f"Host sweep did not lock device rows before the offline "
        f"write; final model={final.model} indicates the host-offline "
        f"write raced the concurrent writer instead of serializing "
        f"through device_locking.lock_devices"
    )
