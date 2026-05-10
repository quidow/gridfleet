"""Phase 3: node_health auto-restart writes desired_state with a transition_token."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.agent_probe_result import ProbeResult
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_node_health_auto_restart_writes_transition_token_with_health_restart_caller(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-health", verified=True, auto_manage=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        pid=88,
        consecutive_health_failures=999,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.services import node_health

    monkeypatch.setattr(node_health, "_restart_node_via_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(node_health, "record_lifecycle_incident", AsyncMock())
    monkeypatch.setattr(node_health.lifecycle_policy, "record_control_action", AsyncMock())
    await node_health._process_node_health(
        db_session,
        node,
        device,
        result=ProbeResult(status="refused", detail="test"),
        grid_device_ids={str(device.id)},
    )
    await db_session.commit()

    await db_session.refresh(node)
    assert node.transition_token is not None
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
        and event.details.get("caller") == "health_restart"
        and event.details.get("transition_token") is not None
        for event in events
    )
