"""Node health auto-restart registers an intent with a transition token."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceEvent, DeviceEventType, DeviceIntent
from app.devices.services import state_write_guard
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_node_health_auto_restart_registers_transition_token_intent(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-health", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=88,
            consecutive_health_failures=999,
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from unittest.mock import Mock

    from app.appium_nodes.services.node_health import NodeHealthService

    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    )
    await svc._process_node_health(
        db_session,
        node,
        device,
        result=ProbeResult(status="refused", detail="test"),
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
        and event.details.get("caller") == "intent_reconciler"
        and event.details.get("transition_token") is not None
        for event in events
    )
    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"auto_recovery:node:{device.id}",
            )
        )
    ).scalar_one()
    assert intent.payload["transition_token"] == str(node.transition_token)


async def test_node_health_skips_escalation_for_intentionally_stopping_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    # I1: a node being intentionally stopped (desired_state=stopped) is still
    # observed_running until the stop propagates. A refused probe in that window
    # is expected teardown, not a health failure — node_health must NOT count it
    # or escalate an auto-recovery restart that fights the stop.
    device = await create_device(db_session, host_id=db_host.id, name="dw-stopping", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            active_connection_target="live",
            desired_state=AppiumDesiredState.stopped,
            pid=88,
            consecutive_health_failures=999,
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from unittest.mock import Mock

    from app.appium_nodes.services.node_health import NodeHealthService

    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    )
    await svc._process_node_health(
        db_session,
        node,
        device,
        result=ProbeResult(status="refused", detail="teardown"),
    )
    await db_session.commit()

    await db_session.refresh(node)
    assert node.transition_token is None  # no restart escalated
    assert node.consecutive_health_failures == 999  # refused probe not counted
    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"auto_recovery:node:{device.id}",
            )
        )
    ).scalar_one_or_none()
    assert intent is None
