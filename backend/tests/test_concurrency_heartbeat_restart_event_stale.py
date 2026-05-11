import asyncio
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device
from app.models.host import Host
from app.services import device_locking, heartbeat
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_ingest_appium_restart_events_skips_when_node_changes_before_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="hb-rs-stale", verified=True)
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.error))
    await db_session.commit()
    device_id = device.id

    about_to_lock = asyncio.Event()
    allow_lock = asyncio.Event()
    original_lock_device = device_locking.lock_device

    async def gated_lock_device(
        session: AsyncSession,
        target_device_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        if target_device_id == device_id:
            about_to_lock.set()
            await asyncio.wait_for(allow_lock.wait(), timeout=2.0)
        return await original_lock_device(session, target_device_id, load_sessions=load_sessions)

    monkeypatch.setattr(device_locking, "lock_device", gated_lock_device)

    health_payload = {
        "appium_processes": {
            "recent_restart_events": [
                {
                    "sequence": 1,
                    "process": "appium",
                    "kind": "restart_succeeded",
                    "port": 4723,
                }
            ]
        }
    }

    async def runner() -> None:
        async with db_session_maker() as session:
            host = await session.get(Host, db_host.id)
            await heartbeat._ingest_appium_restart_events(session, host, health_payload)
            await session.commit()

    async def move_node() -> None:
        await asyncio.wait_for(about_to_lock.wait(), timeout=2.0)
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(port=4724, pid=None, active_connection_target=None, health_running=None, health_state=None)
            )
            await session.commit()
        allow_lock.set()

    await asyncio.gather(runner(), move_node())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.port == 4724
    assert verify_node.state == NodeState.stopped
