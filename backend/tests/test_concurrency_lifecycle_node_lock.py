import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services import lifecycle_policy_actions
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_handle_node_crash_locks_appium_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``handle_node_crash`` writes ``node.state`` and ``node.pid``.
    The AppiumNode row must be locked across those writes.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="lpa-lock",
        operational_state=DeviceOperationalState.busy,
        verified=True,
    )
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()
    original_record_event = lifecycle_policy_actions.record_event

    async def racing_record_event(*args: object, **kwargs: object) -> None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return await original_record_event(*args, **kwargs)

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await session.get(Device, device_id)
            with (
                patch("app.services.lifecycle_policy_actions.record_event", racing_record_event),
                patch(
                    "app.services.lifecycle_policy_actions.stop_managed_node",
                    AsyncMock(side_effect=RuntimeError("stop failed")),
                ),
            ):
                await lifecycle_policy_actions.handle_node_crash(
                    session,
                    target,
                    source="test",
                    reason="test",
                )

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode).where(AppiumNode.device_id == device_id).values(state=NodeState.running, pid=12345)
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.state == NodeState.running, (
        f"Expected running but got {verify_node.state.value} — "
        "handle_node_crash overwrote the concurrent running write "
        "(missing AppiumNode lock)"
    )
    assert verify_node.pid == 12345, (
        f"Expected pid=12345 but got {verify_node.pid} — handle_node_crash overwrote the concurrent pid write"
    )
