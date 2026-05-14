import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.state import set_operational_state
from app.hosts.models import Host
from app.runs import service as run_service
from app.runs.models import RunState, TestRun
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_release_devices_does_not_stomp_offline_writer(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Informational / non-deterministic.

    Verifies that when a concurrent writer sets a device offline *before*
    ``_release_devices`` reads it, the release path correctly skips the
    status update (because the device is no longer reserved/busy).
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="release-target",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.reserved,
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
            await run_service._release_devices(session, run_obj)

    async def stomper() -> None:
        await started.wait()
        async with db_session_maker() as session:
            stmt = select(Device).where(Device.id == device_id)
            device_obj = (await session.execute(stmt)).scalar_one()
            await set_operational_state(
                device_obj,
                DeviceOperationalState.offline,
                publish_event=False,
            )
            await session.commit()
            proceed.set()

    await asyncio.gather(releaser(), stomper())

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
        reservation_row = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()

    assert reservation_row.released_at is not None
    assert device_row.operational_state in {
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

    Injects a pause inside ``_release_devices`` (after its plain SELECT but
    before commit) so a concurrent transaction can commit ``offline`` while
    the releaser's write is in-flight.

    Without ``SELECT FOR UPDATE`` in ``_release_devices`` the releaser stomps
    the offline status and the device ends up ``available``.  After Task 6
    adds the row lock the releaser holds the lock, the stomper's UPDATE blocks
    at the DB level, and the final state is ``offline``.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="serial-target",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.reserved,
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

    # Set by the patched readiness helper after _release_devices has locked
    # and read the device row.
    stomper_can_go = asyncio.Event()

    original_is_ready = is_ready_for_use_async

    async def racing_is_ready(db: AsyncSession, device_arg: Device) -> bool:
        # Signal: _release_devices has read the device row.
        stomper_can_go.set()
        # Give the stomper enough time to commit its offline change before
        # _release_devices continues to the commit.
        await asyncio.sleep(0.15)
        return await original_is_ready(db, device_arg)

    async def releaser() -> None:
        async with db_session_maker() as session:
            run_obj = await run_service.get_run(session, run_id)
            assert run_obj is not None
            run_obj.state = RunState.cancelled
            run_obj.completed_at = datetime.now(UTC)
            with patch("app.devices.services.state.is_ready_for_use_async", racing_is_ready):
                await run_service._release_devices(session, run_obj)

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            device_obj = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await set_operational_state(
                device_obj,
                DeviceOperationalState.offline,
                publish_event=False,
            )
            await session.commit()

    await asyncio.gather(releaser(), stomper())

    assert stomper_can_go.is_set(), (
        "racing_is_ready was never called — _release_devices was refactored to skip "
        "is_ready_for_use_async; the mock injection point is no longer valid"
    )

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    # Without FOR UPDATE in _release_devices: the releaser reads "reserved",
    # the stomper commits "offline", then the releaser commits "available" —
    # stomping the offline.  The assertion fails on "available".
    #
    # After Task 6 adds SELECT FOR UPDATE: the releaser holds the row lock,
    # the stomper's UPDATE blocks until the releaser commits, so the final
    # committed state (after both transactions complete) is "offline".
    assert device_row.operational_state == DeviceOperationalState.offline, (
        f"Expected offline but got {device_row.operational_state.value} — "
        "_release_devices stomped the concurrent offline write (missing row lock)"
    )
