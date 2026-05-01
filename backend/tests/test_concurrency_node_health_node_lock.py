import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import DeviceAvailabilityStatus
from app.models.host import Host
from app.services import node_health
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_node_health_failure_path_locks_appium_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When ``_process_node_health`` enters the auto_manage=False branch and
    writes ``node.state = NodeState.error``, the AppiumNode row must be
    locked.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="nh-lock",
        availability_status=DeviceAvailabilityStatus.busy,
        verified=True,
        auto_manage=False,
    )
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()
    device_id = device.id
    node_id = node.id

    stomper_can_go = asyncio.Event()
    original_record_event = node_health.record_event

    async def racing_record_event(*args: object, **kwargs: object) -> None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return await original_record_event(*args, **kwargs)

    async def health_runner() -> None:
        async with db_session_maker() as session:
            from app.services import device_locking

            locked_device = await device_locking.lock_device(session, device_id)
            with patch("app.services.node_health.record_event", racing_record_event):
                await node_health._process_node_health(
                    session,
                    locked_device.appium_node,
                    locked_device,
                    healthy=False,
                    grid_device_ids=set(),
                )
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            # Use a core UPDATE to guarantee a real SQL statement is always
            # issued, bypassing ORM dirty-tracking (which would skip the UPDATE
            # when the value matches the in-memory snapshot).
            await session.execute(update(AppiumNode).where(AppiumNode.id == node_id).values(state=NodeState.running))
            await session.commit()

    from app.services import control_plane_state_store
    from app.services.settings_service import settings_service

    threshold = int(settings_service.get("general.node_max_failures"))
    for _ in range(threshold - 1):
        await control_plane_state_store.increment_counter(db_session, node_health.NODE_HEALTH_NAMESPACE, str(node.id))
    await db_session.commit()

    await asyncio.gather(health_runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.state == NodeState.running, (
        f"Expected running but got {verify_node.state.value} — "
        "node_health overwrote the concurrent running write (missing AppiumNode lock)"
    )
