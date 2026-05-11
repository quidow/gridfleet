import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.services import device_locking, maintenance_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_enter_maintenance_writes_stop_intent_without_inline_agent_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-relock",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            state=NodeState.running,
        )
    )
    await db_session.commit()
    device_id = device.id

    target = await device_locking.lock_device(db_session, device_id)
    await maintenance_service.enter_maintenance(db_session, target)

    final_status = (
        await db_session.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
    ).one()
    node_status = (
        await db_session.execute(
            select(AppiumNode.state, AppiumNode.desired_state).where(AppiumNode.device_id == device_id)
        )
    ).one()

    assert final_status.operational_state == DeviceOperationalState.available
    assert final_status.hold == DeviceHold.maintenance
    assert node_status.state == NodeState.running
    assert node_status.desired_state == NodeState.stopped
