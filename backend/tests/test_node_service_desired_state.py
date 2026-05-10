"""Phase 3 dual-write tests for node_service managed-node actions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.node_service import restart_node, start_node, stop_node
from app.services.node_service_types import TemporaryNodeHandle
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_start_node_writes_desired_running_before_inline_rpc(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-start", verified=True)
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_mark_node_started(
        _db: AsyncSession,
        dev: Device,
        **kwargs: object,
    ) -> AppiumNode:
        assert dev.appium_node is not None
        return dev.appium_node

    handle = TemporaryNodeHandle(
        port=4723,
        pid=1234,
        active_connection_target=device.identity_value,
        agent_base="http://agent",
        owner_key=f"device:{device.id}",
    )

    with (
        patch("app.services.node_service.start_temporary_node", new=AsyncMock(return_value=handle)),
        patch("app.services.node_service.mark_node_started", new=fake_mark_node_started),
    ):
        await start_node(db_session, device, caller="operator_route")

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
    assert events[0].details["new_desired_state"] == "running"
    assert events[0].details["caller"] == "operator_route"


async def test_stop_node_writes_desired_stopped_before_inline_rpc(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-stop", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        pid=999,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    with (
        patch("app.services.node_service.stop_temporary_node", new=AsyncMock(return_value=True)),
        patch("app.services.node_service.mark_node_stopped", new=AsyncMock(return_value=node)),
    ):
        await stop_node(db_session, device, caller="operator_route")

    await db_session.refresh(node)
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
    assert events[0].details["new_desired_state"] == "stopped"
    assert events[0].details["caller"] == "operator_route"


async def test_restart_node_writes_transition_token_and_desired_running(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-restart", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        pid=42,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    fake_handle = TemporaryNodeHandle(
        port=4723,
        pid=43,
        active_connection_target=None,
        agent_base="http://a",
        owner_key=f"device:{device.id}",
    )
    with (
        patch("app.services.node_service.stop_temporary_node", new=AsyncMock(return_value=True)),
        patch("app.services.node_service.mark_node_stopped", new=AsyncMock(return_value=node)),
        patch("app.services.node_service._start_with_owner", new=AsyncMock(return_value=fake_handle)),
        patch("app.services.node_service.mark_node_started", new=AsyncMock(return_value=node)),
    ):
        await restart_node(db_session, device, caller="operator_restart")

    await db_session.refresh(node)
    assert node.transition_token is not None
    assert node.transition_deadline is not None
    assert node.desired_state == NodeState.running

    events = (
        (
            await db_session.execute(
                select(DeviceEvent)
                .where(
                    DeviceEvent.device_id == device.id, DeviceEvent.event_type == DeviceEventType.desired_state_changed
                )
                .order_by(DeviceEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert events[-1].details is not None
    assert events[-1].details["caller"] == "operator_restart"
    assert events[-1].details["transition_token"] is not None
