"""Phase 3 lifecycle-policy desired-state caller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_attempt_auto_recovery_tags_desired_state_with_lifecycle_recovery(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-recover", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.stopped,
        desired_state=NodeState.stopped,
    )
    db_session.add(node)
    await db_session.commit()

    captured: dict[str, object] = {}

    async def fake_start(_db: AsyncSession, _device: Device, *, caller: str = "operator_route") -> AppiumNode:
        captured["caller"] = caller
        return node

    from app.services import lifecycle_policy

    with patch.object(lifecycle_policy, "start_managed_node", new=fake_start):
        await lifecycle_policy.attempt_auto_recovery(
            db_session,
            device,
            source="health_recovery",
            reason="test",
        )

    assert captured.get("caller") == "lifecycle_recovery"


async def test_handle_node_crash_tags_desired_state_with_lifecycle_crash(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        pid=99,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import lifecycle_policy_actions

    captured: dict[str, object] = {}

    async def fake_stop(_db: AsyncSession, _device: Device, *, caller: str = "operator_route") -> AppiumNode:
        captured["caller"] = caller
        return node

    with patch.object(lifecycle_policy_actions, "stop_managed_node", new=fake_stop):
        await lifecycle_policy_actions.handle_node_crash(
            db_session,
            device,
            source="connectivity_lost",
            reason="agent disconnected",
        )

    assert captured.get("caller") == "lifecycle_crash"


async def test_handle_node_crash_writes_desired_stopped_when_node_already_stopped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash-stopped", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.stopped,
        desired_state=NodeState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import lifecycle_policy_actions

    await lifecycle_policy_actions.handle_node_crash(
        db_session,
        device,
        source="health_check_fail",
        reason="probe failed",
    )

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].details is not None
    assert events[0].details["caller"] == "lifecycle_crash"
    assert events[0].details["new_desired_state"] == "stopped"
