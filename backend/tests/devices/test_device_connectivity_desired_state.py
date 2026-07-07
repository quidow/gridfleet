"""Connectivity loss parks the node (defer-stop synthesized from device_checks_healthy)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import DeviceEvent, DeviceEventType
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent_synthesis import synthesize_fact_intents
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_stop_disconnected_node_parks_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-conn", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=77,
    )
    device.device_checks_healthy = False
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.devices.services import connectivity as device_connectivity

    await device_connectivity._stop_disconnected_node(
        db_session, device, health=DeviceHealthService(publisher=event_bus)
    )
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

    # Connectivity defer-stop is no longer stored — it is synthesized from
    # device_checks_healthy IS FALSE.
    await db_session.refresh(device)
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    intent = next(i for i in intents if i.source == f"connectivity:{device.id}")
    assert intent.axis == "node_process"
    assert intent.payload["stop_mode"] == "defer"
