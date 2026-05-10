"""Phase 3: verification refresh path writes desired_state='running'."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.appium_node import NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.node_service_types import TemporaryNodeHandle
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_retain_verified_node_writes_desired_running_with_verification_caller(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-verify", verified=True)
    await db_session.commit()

    handle = TemporaryNodeHandle(
        port=4724,
        pid=55,
        active_connection_target=device.identity_value,
        agent_base="http://a",
        owner_key="device:abc",
    )
    job: dict[str, object] = {}

    from app.services import device_verification_execution

    monkeypatch.setattr(device_verification_execution, "set_stage", AsyncMock())
    monkeypatch.setattr(
        device_verification_execution.appium_node_resource_service,
        "transfer_temporary_to_managed",
        AsyncMock(return_value=1),
    )
    await device_verification_execution.retain_verified_node(job, db_session, device, handle)

    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.desired_state == NodeState.running

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
    assert any(
        event.details is not None
        and event.details.get("caller") == "verification"
        and event.details.get("new_desired_state") == "running"
        for event in events
    )
