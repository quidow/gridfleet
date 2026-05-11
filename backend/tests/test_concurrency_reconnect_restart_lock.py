import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.routers import devices_control
from app.services import device_locking, maintenance_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_reconnect_restart_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="reconnect-maintenance-race",
        operational_state=DeviceOperationalState.offline,
        connection_type="network",
        ip_address="10.0.0.50",
        verified=True,
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
    await db_session.commit()
    device_id = device.id

    async def fake_lifecycle_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}

    restart_entered = asyncio.Event()
    allow_restart = asyncio.Event()

    async def fake_restart_node(db: AsyncSession, dev: Device, *, caller: str = "operator_restart") -> AppiumNode:
        assert dev.appium_node is not None
        restart_entered.set()
        await asyncio.wait_for(allow_restart.wait(), timeout=2.0)
        dev.appium_node.desired_state = AppiumDesiredState.running
        await db.commit()
        return dev.appium_node

    monkeypatch.setattr(devices_control, "pack_device_lifecycle_action", fake_lifecycle_action)
    monkeypatch.setattr(devices_control, "restart_managed_node", fake_restart_node)

    async def reconnect() -> None:
        async with db_session_maker() as session:
            await devices_control.reconnect_device(device_id, db=session)

    async def enter_maintenance_before_restart() -> None:
        await asyncio.wait_for(restart_entered.wait(), timeout=2.0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            await maintenance_service.enter_maintenance(session, locked)
        allow_restart.set()

    await asyncio.gather(reconnect(), enter_maintenance_before_restart())

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
        ).one()

    assert final.operational_state == DeviceOperationalState.offline
    assert final.hold == DeviceHold.maintenance
