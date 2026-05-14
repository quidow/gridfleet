"""Connectivity loss registers a routing-blocking stop intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceEvent, DeviceEventType, DeviceIntent
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_stop_disconnected_node_registers_connectivity_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-conn", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=77,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.devices.services import connectivity as device_connectivity

    await device_connectivity._stop_disconnected_node(db_session, device)
    await db_session.commit()

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
    assert {event.details.get("field") for event in events if event.details is not None} == {
        "accepting_new_sessions",
        "stop_pending",
    }
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True
    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"connectivity:{device.id}",
            )
        )
    ).scalar_one()
    assert intent.axis == "node_process"
    assert intent.payload["stop_mode"] == "defer"
