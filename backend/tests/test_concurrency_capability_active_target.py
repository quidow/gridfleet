import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceType
from app.devices.services import capability as capability_service
from app.hosts.models import Host
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_get_live_active_connection_target_locks_device_and_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``_get_live_active_connection_target`` writes ``node.active_connection_target``
    and flushes. Both Device and AppiumNode rows must be locked across that write
    so concurrent writers (e.g. ``mark_node_started`` / ``mark_node_stopped``) cannot
    silently overwrite or be overwritten by the helper.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="cap-lock",
        device_type=DeviceType.emulator,
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
            active_connection_target=None,
        )
    )
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()

    async def racing_active_target(db: AsyncSession, dev: Device) -> str | None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return "udid-from-host"

    async def runner() -> None:
        async with db_session_maker() as session:
            from sqlalchemy.orm import selectinload

            stmt = (
                select(Device)
                .where(Device.id == device_id)
                .options(selectinload(Device.appium_node), selectinload(Device.host))
            )
            target = (await session.execute(stmt)).scalar_one()
            with patch(
                "app.devices.services.capability._active_target_from_host_snapshot",
                racing_active_target,
            ):
                await capability_service._get_live_active_connection_target(session, target)
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(active_connection_target="udid-from-stomper")
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.active_connection_target == "udid-from-stomper", (
        f"Expected udid-from-stomper but got {verify_node.active_connection_target!r} — "
        "_get_live_active_connection_target overwrote the concurrent stomper write "
        "(missing AppiumNode lock)"
    )
