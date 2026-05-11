"""Phase 3: exit_maintenance records desired_state='running' immediately."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import DeviceHold
from app.models.device_event import DeviceEvent, DeviceEventType
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_exit_maintenance_writes_desired_running_when_node_present(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-maint", verified=True, auto_manage=True)
    device.hold = DeviceHold.maintenance
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        desired_port=None,
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.services import maintenance_service

    monkeypatch.setattr(maintenance_service, "schedule_device_recovery", AsyncMock())
    await maintenance_service.exit_maintenance(db_session, device)

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
    assert events[0].details["caller"] == "maintenance_exit"
    assert events[0].details["new_desired_state"] == "running"


async def test_enter_maintenance_writes_desired_stopped_and_returns_without_waiting_for_agent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="m-enter", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=1,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.services import maintenance_service

    await maintenance_service.enter_maintenance(db_session, device)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.state == AppiumDesiredState.running
