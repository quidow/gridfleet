import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import device_connectivity
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_stop_disconnected_node_locks_device_and_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``_stop_disconnected_node`` writes ``node.state``. Both the Device row
    and the AppiumNode row must be locked across that write.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="dc-lock",
        availability_status=DeviceAvailabilityStatus.busy,
        verified=True,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running))
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()

    async def fake_stop_via_agent(_device: object, _node: object) -> bool:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return True

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await session.get(Device, device_id)
            with patch(
                "app.services.device_connectivity._stop_node_via_agent",
                fake_stop_via_agent,
            ):
                await device_connectivity._stop_disconnected_node(session, target)
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode).where(AppiumNode.device_id == device_id).values(state=NodeState.error)
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.state == NodeState.error, (
        f"Expected error but got {verify_node.state.value} — "
        "_stop_disconnected_node overwrote the concurrent error write "
        "(missing AppiumNode lock)"
    )
