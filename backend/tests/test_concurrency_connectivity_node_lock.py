import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import connectivity as device_connectivity
from app.hosts.models import Host
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_stop_disconnected_node_locks_device_and_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``_stop_disconnected_node`` writes node desired state. Both the Device row
    and the AppiumNode row must be locked across that write.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="dc-lock",
        operational_state=DeviceOperationalState.busy,
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

    stomper_can_go = asyncio.Event()

    real_register = device_connectivity.register_intents_and_reconcile

    async def fake_register_intents_and_reconcile(
        db: AsyncSession,
        *,
        device_id: object,
        intents: object,
        reason: str,
    ) -> None:
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        await real_register(db, device_id=device_id, intents=intents, reason=reason)

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await session.get(Device, device_id)
            with patch(
                "app.devices.services.connectivity.register_intents_and_reconcile",
                fake_register_intents_and_reconcile,
            ):
                await device_connectivity._stop_disconnected_node(session, target)
            await session.commit()

    async def stomper() -> None:
        await stomper_can_go.wait()
        async with db_session_maker() as session:
            await session.execute(
                update(AppiumNode)
                .where(AppiumNode.device_id == device_id)
                .values(health_running=False, health_state="error")
            )
            await session.commit()

    await asyncio.gather(runner(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert verify_node.desired_state == AppiumDesiredState.running
    assert verify_node.stop_pending is True
