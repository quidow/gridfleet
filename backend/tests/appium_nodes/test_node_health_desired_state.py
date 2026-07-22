"""Node health auto-restart registers an intent with a restart watermark."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.node_health import _NodeObservation
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceEvent, DeviceEventType
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.health import DeviceHealthService
from app.lifecycle.services import remediation_log
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_node_health_auto_restart_registers_restart_watermark_intent(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-health", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=88,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.appium_nodes.services.node_health import NodeHealthService

    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.node_fail_window_sec": 0}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=now_utc())
    await svc._process_node_health(
        db_session,
        node,
        locked,
        snapshot,
        observation=_NodeObservation(ProbeResult(status="refused", detail="test")),
    )
    await db_session.commit()

    await db_session.refresh(node)
    assert node.restart_requested_at is not None
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
        and event.details.get("restart_requested_at") is not None
        for event in events
    )
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == remediation_log.DIRECTIVE_START
    assert ladder.node_directive.restart_watermark == node.restart_requested_at


async def test_node_health_skips_escalation_for_intentionally_stopping_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    # I1: a node being intentionally stopped (desired_state=stopped) is still
    # observed_running until the stop propagates. A refused probe in that window
    # is expected teardown, not a health failure — node_health must NOT count it
    # or escalate an auto-recovery restart that fights the stop.
    device = await create_device(db_session, host_id=db_host.id, name="dw-stopping", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        active_connection_target="live",
        desired_state=AppiumDesiredState.stopped,
        pid=88,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    from app.appium_nodes.services.node_health import NodeHealthService

    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=now_utc())
    await svc._process_node_health(
        db_session,
        node,
        locked,
        snapshot,
        observation=_NodeObservation(ProbeResult(status="refused", detail="teardown")),
    )
    await db_session.commit()

    await db_session.refresh(node)
    assert node.restart_requested_at is None  # no restart escalated
    assert node.health_failing_since is None  # refused probe not counted
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None
