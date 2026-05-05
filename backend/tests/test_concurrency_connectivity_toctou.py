"""Verify device_connectivity availability re-checks after row locks."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceOperationalState
from app.models.host import Host, HostStatus
from app.services import device_connectivity, device_locking
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_offline_write_skips_when_device_enters_active_state_before_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The offline-write path must re-read status after lock acquisition."""
    db_host.status = HostStatus.online
    await db_session.commit()

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="conn-offline-recheck",
        connection_target="missing-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
        auto_manage=True,
    )
    device_id = device.id

    lock_attempted = asyncio.Event()
    racer_done = asyncio.Event()
    original_lock = device_locking.lock_device

    async def gated_lock(db: AsyncSession, did: object, **kw: object) -> Device:
        if did == device_id:
            lock_attempted.set()
            await asyncio.wait_for(racer_done.wait(), timeout=2.0)
        return await original_lock(db, did, **kw)

    async def runner() -> None:
        with (
            patch("app.services.device_connectivity._get_agent_devices", new=AsyncMock(return_value=set())),
            patch("app.services.device_connectivity._get_lifecycle_state", new=AsyncMock(return_value=None)),
            patch("app.services.device_connectivity._uses_endpoint_health", new=AsyncMock(return_value=False)),
            patch("app.services.device_connectivity._stop_disconnected_node", new=AsyncMock(return_value=None)),
            patch("app.services.device_connectivity.device_health.update_device_checks", new=AsyncMock()),
            patch("app.services.device_connectivity.record_event", new=AsyncMock()),
            patch("app.services.device_connectivity.device_locking.lock_device", side_effect=gated_lock),
        ):
            async with db_session_maker() as session:
                await device_connectivity._check_connectivity(session)

    async def racer() -> None:
        await asyncio.wait_for(lock_attempted.wait(), timeout=2.0)
        async with db_session_maker() as session:
            locked = await original_lock(session, device_id)
            locked.operational_state = DeviceOperationalState.busy
            await session.commit()
        racer_done.set()

    await asyncio.gather(runner(), racer())

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).scalar_one()

    assert final == DeviceOperationalState.busy, (
        f"Expected busy but got {final.value} - _check_connectivity overwrote "
        "a concurrent active-state transition with stale offline status"
    )


async def test_active_state_lifecycle_write_skips_when_device_leaves_active_state_before_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The active-state path must re-read status before lifecycle writes."""
    db_host.status = HostStatus.online
    await db_session.commit()

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="conn-active-recheck",
        connection_target="missing-active-target",
        operational_state=DeviceOperationalState.busy,
        verified=True,
        auto_manage=True,
    )
    device_id = device.id

    lock_attempted = asyncio.Event()
    racer_done = asyncio.Event()
    original_lock = device_locking.lock_device
    note_connectivity_loss = AsyncMock()

    async def gated_lock(db: AsyncSession, did: object, **kw: object) -> Device:
        if did == device_id:
            lock_attempted.set()
            await asyncio.wait_for(racer_done.wait(), timeout=2.0)
        return await original_lock(db, did, **kw)

    async def runner() -> None:
        with (
            patch("app.services.device_connectivity._get_agent_devices", new=AsyncMock(return_value=set())),
            patch("app.services.device_connectivity._get_lifecycle_state", new=AsyncMock(return_value=None)),
            patch("app.services.device_connectivity._uses_endpoint_health", new=AsyncMock(return_value=False)),
            patch("app.services.device_connectivity._stop_disconnected_node", new=AsyncMock(return_value=None)),
            patch("app.services.device_connectivity.device_health.update_device_checks", new=AsyncMock()),
            patch("app.services.device_connectivity.record_event", new=AsyncMock()),
            patch(
                "app.services.device_connectivity.lifecycle_policy.note_connectivity_loss",
                new=note_connectivity_loss,
            ),
            patch("app.services.device_connectivity.device_locking.lock_device", side_effect=gated_lock),
        ):
            async with db_session_maker() as session:
                await device_connectivity._check_connectivity(session)

    async def racer() -> None:
        await asyncio.wait_for(lock_attempted.wait(), timeout=2.0)
        async with db_session_maker() as session:
            locked = await original_lock(session, device_id)
            locked.operational_state = DeviceOperationalState.available
            await session.commit()
        racer_done.set()

    await asyncio.gather(runner(), racer())

    note_connectivity_loss.assert_not_awaited()

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).scalar_one()

    assert final == DeviceOperationalState.available
