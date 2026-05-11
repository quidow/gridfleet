import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.host import Host
from app.services import heartbeat
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_ingest_appium_restart_events_locks_device_and_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When ``_ingest_appium_restart_events`` processes a ``restart_succeeded``
    event and clears node health error state, both the Device row and the
    AppiumNode row must be locked.
    """
    device = await create_device(db_session, host_id=db_host.id, name="hb-rs-lock", verified=True)
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.error))
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()
    original_record_event = heartbeat.record_event

    async def racing_record_event(*args: object, **kwargs: object) -> None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        return await original_record_event(*args, **kwargs)

    health_payload = {
        "appium_processes": {
            "recent_restart_events": [
                {
                    "sequence": 1,
                    "process": "appium",
                    "kind": "restart_succeeded",
                    "port": 4723,
                    # No ``pid`` field: avoids an incidental ORM flush of ``node.pid``
                    # that would otherwise create an implicit row lock before
                    # ``record_event`` is reached. We want the test to expose the
                    # window where node health state is mutated without a proper
                    # ``SELECT ... FOR UPDATE``.
                }
            ]
        }
    }

    async def runner() -> None:
        async with db_session_maker() as session:
            host = await session.get(Host, db_host.id)
            with patch("app.services.heartbeat.record_event", racing_record_event):
                await heartbeat._ingest_appium_restart_events(session, host, health_payload)
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(health_running=False, health_state=NodeState.error.value)
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.health_state == NodeState.error.value, (
        f"Expected error but got {verify_node.health_state} - "
        "_ingest_appium_restart_events overwrote the concurrent error write "
        "(missing AppiumNode lock)"
    )
