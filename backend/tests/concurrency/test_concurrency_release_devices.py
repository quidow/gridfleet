from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.state import derive_operational_state
from app.runs import service as run_service
from app.runs import service_reservation as run_reservation_service
from app.runs.models import RunState, TestRun
from app.runs.service_lifecycle_release import RunReleaseService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.hosts.models import Host
    from app.packs.services.catalog_view import PackView

_settings = FakeSettingsReader({})
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    deferred_stop=AsyncMock(),
)

pytestmark = pytest.mark.asyncio


async def _release(session: AsyncSession, run_obj: TestRun) -> list:
    """Acquire the run's device proofs then release — the caller commits."""
    locked_by_id = await _release_svc.lock_run_devices(session, run_obj)
    return await _release_svc.release_devices(session, run_obj, locked_by_id=locked_by_id)


async def test_release_devices_does_not_stomp_offline_writer(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Informational / non-deterministic."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="release-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name="r",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    reservation = DeviceReservation(
        run=run,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    db_session.add(reservation)
    await db_session.commit()
    device_id = device.id
    run_id = run.id

    started = asyncio.Event()
    proceed = asyncio.Event()

    async def releaser() -> None:
        async with db_session_maker() as session:
            run_obj = await run_service.get_run(session, run_id)
            assert run_obj is not None
            run_obj.state = RunState.cancelled
            run_obj.completed_at = datetime.now(UTC)
            started.set()
            await proceed.wait()
            await _release(session, run_obj)
            await session.commit()

    async def stomper() -> None:
        await started.wait()
        async with db_session_maker() as session:
            stmt = select(Device).where(Device.id == device_id)
            device_obj = (await session.execute(stmt)).scalar_one()
            device_obj.device_checks_healthy = False
            await session.commit()
            proceed.set()

    await asyncio.gather(releaser(), stomper())

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
        reservation_row = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()

        assert reservation_row.released_at is not None
        derived = await derive_operational_state(verify, device_row, now=datetime.now(UTC))
        assert derived in {
            DeviceOperationalState.available,
            DeviceOperationalState.offline,
        }


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_release_devices_serializes_with_concurrent_writer(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Substantive deterministic race test.

    ``release_devices`` locks each reserved device FOR UPDATE (via
    ``lock_run_devices``) before its reconcile pass, so a concurrent
    ``offline`` writer blocks at the DB level and the final derived state is
    ``offline`` — the releaser never stomps it.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="serial-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name="r",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run=run,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    await db_session.commit()
    device_id = device.id
    run_id = run.id

    stomper_can_go = asyncio.Event()

    original_loader = load_device_decision_snapshot

    async def racing_snapshot(
        db: AsyncSession,
        locked: LockedDevice,
        *,
        packs: Mapping[str, PackView] | None = None,
        now: datetime,
    ) -> DeviceDecisionSnapshot:
        if not stomper_can_go.is_set():
            stomper_can_go.set()
            await asyncio.sleep(0.15)
        return await original_loader(db, locked, packs=packs, now=now)

    async def releaser() -> None:
        async with db_session_maker() as session:
            run_obj = await run_service.get_run(session, run_id)
            assert run_obj is not None
            run_obj.state = RunState.cancelled
            run_obj.completed_at = datetime.now(UTC)
            with patch(
                "app.devices.services.intent_reconciler.load_device_decision_snapshot",
                racing_snapshot,
            ):
                await _release(session, run_obj)
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            device_obj = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            device_obj.device_checks_healthy = False
            await session.commit()

    await asyncio.gather(releaser(), stomper())

    assert stomper_can_go.is_set(), (
        "racing_snapshot was never called — release_devices' reconcile pass no longer "
        "loads the device decision snapshot; the mock injection point is no longer valid"
    )

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
        derived = await derive_operational_state(verify, device_row, now=datetime.now(UTC))
    assert derived == DeviceOperationalState.offline, (
        f"Expected offline but got {derived.value} — "
        "release_devices stomped the concurrent offline write (missing row lock)"
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_release_devices_skips_a_reservation_whose_device_vanished(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The ``locked is None`` fallback in ``release_devices``, driven for real.

    ``lock_run_devices`` returns a proof per Device row it actually locked, so a
    reservation gets no proof only when its Device row is gone by lock time.
    ``device_reservations.device_id`` is ``ON DELETE CASCADE``, so the same
    delete took the reservation row with it: the branch can only ever be reached
    for a reservation that no longer exists, and writing release fields to it
    would be an unlocked write onto a phantom row that no UPDATE can match.
    Skipping is the only outcome that commits.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="vanished-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name="vanished",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run=run,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    await db_session.commit()
    device_id = device.id
    run_id = run.id

    async with db_session_maker() as releaser:
        run_obj = await run_service.get_run(releaser, run_id)
        assert run_obj is not None
        assert [entry.device_id for entry in run_obj.device_reservations] == [device_id]
        run_obj.state = RunState.cancelled
        run_obj.completed_at = datetime.now(UTC)

        # A concurrent delete lands between the reservation load and the lock
        # pass; the FK cascade removes the reservation row in the same commit.
        async with db_session_maker() as deleter:
            await deleter.execute(sa_delete(Device).where(Device.id == device_id))
            await deleter.commit()

        locked_by_id = await _release_svc.lock_run_devices(releaser, run_obj)
        assert locked_by_id == {}, "the vanished Device must yield no proof"
        assert await _release_svc.release_devices(releaser, run_obj, locked_by_id=locked_by_id) == []
        await releaser.commit()

    async with db_session_maker() as verify:
        assert (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one_or_none() is None


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_reservation_lock_before_release_is_deadlock_free(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Device-before-child (transaction A) and run -> device -> child (the release
    path, transaction B) share the same acquisition order, so a device-exclude
    that holds the Device + reservation cannot deadlock a concurrent run release
    that waits for the same Device. Both finish; the released row is never
    excluded."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="lockorder-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name="lockorder",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run=run,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    await db_session.commit()
    device_id = device.id
    run_id = run.id

    a_holds_locks = asyncio.Event()
    b_reached_lock = asyncio.Event()

    async def txn_a() -> None:
        async with db_session_maker() as a:
            locked = await device_locking.lock_device_handle(a, device_id)
            entry = await run_reservation_service.lock_active_reservation(a, locked)
            assert entry is not None
            assert entry.excluded is False
            a_holds_locks.set()
            await b_reached_lock.wait()
            await asyncio.sleep(0.1)  # let B block on the Device lock before A releases
            await a.commit()

    async def txn_b() -> None:
        async with db_session_maker() as b:
            run_obj = await run_service.get_run(b, run_id)
            assert run_obj is not None
            run_obj.state = RunState.cancelled
            run_obj.completed_at = datetime.now(UTC)
            await a_holds_locks.wait()
            b_reached_lock.set()
            await _release(b, run_obj)
            await b.commit()

    await asyncio.wait_for(asyncio.gather(txn_a(), txn_b()), timeout=10.0)

    async with db_session_maker() as verify:
        reservation_row = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()
        assert reservation_row.released_at is not None
        assert reservation_row.excluded is False
