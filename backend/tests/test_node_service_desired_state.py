"""Phase 3 dual-write tests for node_service managed-node actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.node_service import restart_node, start_node, stop_node
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_start_node_writes_desired_running_before_inline_rpc(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-start", verified=True)
    await db_session.refresh(device, attribute_names=["appium_node"])

    result = await start_node(db_session, device, caller="operator_route")

    assert result.desired_state == AppiumDesiredState.running
    assert result.state == AppiumDesiredState.stopped
    assert result.desired_port is not None

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
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=999,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    result = await stop_node(db_session, device, caller="operator_route")

    await db_session.refresh(node)
    assert result.desired_state == AppiumDesiredState.stopped
    assert node.state == AppiumDesiredState.running
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
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=42,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    result = await restart_node(db_session, device, caller="operator_restart")

    await db_session.refresh(node)
    assert result.state == AppiumDesiredState.running
    assert node.transition_token is not None
    assert node.transition_deadline is not None
    assert node.desired_state == AppiumDesiredState.running

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
