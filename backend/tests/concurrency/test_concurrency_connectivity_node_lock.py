import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import connectivity as device_connectivity
from app.devices.services import state_write_guard
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent_reconciler import reconcile_device as real_reconcile_device
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

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
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=0,
                active_connection_target="",
            )
        )
    # The connectivity defer-stop (node.stop_pending=True) is synthesized from
    # device_checks_healthy IS FALSE, so the reconcile must observe this durable fact.
    device.device_checks_healthy = False
    await db_session.commit()
    device_id = device.id

    stomper_can_go = asyncio.Event()

    async def fake_reconcile(
        db: object,
        device_id: object,
        *,
        publisher: object,
        observed_reason: object = None,
    ) -> None:
        # Runs inside _lock_mutate_reconcile while the Device AND AppiumNode rows are
        # locked, so the stomper's concurrent AppiumNode UPDATE blocks until we commit.
        stomper_can_go.set()
        await asyncio.sleep(0.15)
        await real_reconcile_device(db, device_id, publisher=publisher, observed_reason=observed_reason)

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await session.get(Device, device_id)
            with patch("app.devices.services.intent.reconcile_device", fake_reconcile):
                await device_connectivity._stop_disconnected_node(
                    session, target, health=DeviceHealthService(publisher=event_bus)
                )
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
