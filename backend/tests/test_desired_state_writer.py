"""Unit tests for the Phase 3 desired-state writer helper."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app import metrics_recorders
from app.models.appium_node import AppiumNode, NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.services.desired_state_writer import write_desired_state
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_write_desired_state_running_mutates_node_and_records_event(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-1", verified=True)
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.stopped)
    db_session.add(node)
    await db_session.flush()

    before = metrics_recorders.APPIUM_DESIRED_STATE_WRITES.labels(
        caller="operator_route", target_state="running"
    )._value.get()

    await write_desired_state(
        db_session,
        node=node,
        target=NodeState.running,
        caller="operator_route",
        desired_port=4723,
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_state == NodeState.running
    assert node.desired_port == 4723
    assert node.transition_token is None

    events = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == DeviceEventType.desired_state_changed
    assert events[0].details is not None
    assert events[0].details["new_desired_state"] == "running"
    assert events[0].details["caller"] == "operator_route"
    assert events[0].details["desired_port"] == 4723

    after = metrics_recorders.APPIUM_DESIRED_STATE_WRITES.labels(
        caller="operator_route", target_state="running"
    )._value.get()
    assert after == before + 1


async def test_write_desired_state_stopped_clears_desired_port_and_token(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-2", verified=True)
    token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    db_session.add(node)
    await db_session.flush()

    await write_desired_state(
        db_session,
        node=node,
        target=NodeState.stopped,
        caller="connectivity",
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_state == NodeState.stopped
    assert node.desired_port is None
    assert node.transition_token is None
    assert node.transition_deadline is None


async def test_write_desired_state_with_transition_token_increments_token_counter(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-3", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.flush()

    before = metrics_recorders.APPIUM_TRANSITION_TOKEN_WRITES.labels(caller="operator_restart")._value.get()
    token = uuid.uuid4()

    await write_desired_state(
        db_session,
        node=node,
        target=NodeState.running,
        caller="operator_restart",
        desired_port=4723,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.transition_token == token
    assert node.transition_deadline is not None

    after = metrics_recorders.APPIUM_TRANSITION_TOKEN_WRITES.labels(caller="operator_restart")._value.get()
    assert after == before + 1


async def test_write_desired_state_overrides_pending_token_increments_overridden_counter(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-4", verified=True)
    existing_token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        transition_token=existing_token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(
        DeviceEvent(
            device_id=device.id,
            event_type=DeviceEventType.desired_state_changed,
            details={"caller": "operator_restart", "transition_token": str(existing_token)},
        )
    )
    await db_session.flush()

    before = metrics_recorders.APPIUM_TRANSITION_TOKEN_OVERRIDDEN.labels(
        losing_source="operator_restart", winning_source="health_restart"
    )._value.get()

    await write_desired_state(
        db_session,
        node=node,
        target=NodeState.running,
        caller="health_restart",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    await db_session.commit()

    after = metrics_recorders.APPIUM_TRANSITION_TOKEN_OVERRIDDEN.labels(
        losing_source="operator_restart", winning_source="health_restart"
    )._value.get()
    assert after == before + 1
