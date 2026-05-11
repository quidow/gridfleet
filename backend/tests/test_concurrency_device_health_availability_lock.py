from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.services import device_health, device_locking
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_health_failure_offline_write_serializes_with_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-offline-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    async def health_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await device_health.update_device_checks(
                session,
                loaded,
                healthy=False,
                summary="Disconnected",
            )
            await session.commit()

    async def reservation_writer() -> None:
        await asyncio.sleep(0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.hold = DeviceHold.reserved
            await session.commit()

    await asyncio.wait_for(asyncio.gather(health_writer(), reservation_writer()), timeout=5.0)

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
        ).one()

    assert final.operational_state == DeviceOperationalState.offline
    assert final.hold == DeviceHold.reserved


async def test_health_recovery_available_write_serializes_with_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-recovery-race",
        operational_state=DeviceOperationalState.offline,
        verified=True,
        auto_manage=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    )
    device.device_checks_healthy = True
    device.session_viability_status = "passed"
    await db_session.commit()
    device_id = device.id

    async def recovery_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await session.refresh(loaded, ["appium_node"])
            await device_health.apply_node_state_transition(
                session,
                loaded,
                health_running=None,
                health_state=None,
                mark_offline=False,
            )
            await session.commit()

    async def maintenance_writer() -> None:
        await asyncio.sleep(0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.hold = DeviceHold.maintenance
            await session.commit()

    await asyncio.wait_for(asyncio.gather(recovery_writer(), maintenance_writer()), timeout=5.0)

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
        ).one()

    assert final.operational_state == DeviceOperationalState.available
    assert final.hold == DeviceHold.maintenance
