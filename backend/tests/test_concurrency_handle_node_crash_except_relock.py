import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import appium_node_locking, device_locking, lifecycle_policy_actions
from app.services.node_service_types import NodeManagerError
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


class _StopNodeCommitsThenRaises:
    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        assert device.appium_node is not None
        device.availability_status = DeviceAvailabilityStatus.offline
        await db.commit()
        raise NodeManagerError("simulated stop_node failure after commit")


async def test_handle_node_crash_relocks_after_stop_node_commit_in_except_branch(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="lpa-except-relock",
        availability_status=DeviceAvailabilityStatus.busy,
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

    original_lock_device = device_locking.lock_device
    original_lock_node = appium_node_locking.lock_appium_node_for_device
    device_lock_count = 0
    node_lock_count = 0

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        nonlocal device_lock_count
        if target_id == device_id:
            device_lock_count += 1
        return await original_lock_device(db, target_id, load_sessions=load_sessions)

    async def observed_lock_node(db: AsyncSession, target_id: uuid.UUID) -> AppiumNode | None:
        nonlocal node_lock_count
        if target_id == device_id:
            node_lock_count += 1
        return await original_lock_node(db, target_id)

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)
    monkeypatch.setattr(appium_node_locking, "lock_appium_node_for_device", observed_lock_node)

    async with db_session_maker() as session:
        target = await session.get(Device, device_id)
        assert target is not None
        await lifecycle_policy_actions.handle_node_crash(
            session,
            target,
            source="test",
            reason="simulated failure",
            manager_resolver=lambda _device: _StopNodeCommitsThenRaises(),
        )

    assert device_lock_count >= 2, "Device was not re-locked after stop_node committed and raised"
    assert node_lock_count >= 2, "AppiumNode was not re-locked after stop_node committed and raised"

    async with db_session_maker() as verify:
        final_device = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
        final_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert final_device.availability_status == DeviceAvailabilityStatus.offline
    assert final_node.state == NodeState.error
    assert final_node.pid is None
