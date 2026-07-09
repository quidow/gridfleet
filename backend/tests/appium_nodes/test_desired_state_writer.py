"""Unit tests for the Phase 3 desired-state writer helper."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.core import metrics_recorders
from app.devices.models import DeviceEvent, DeviceEventType
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_write_desired_state_running_mutates_node_and_records_event(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-1", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        pid=None,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.flush()

    before = metrics_recorders.APPIUM_DESIRED_STATE_WRITES.labels(
        caller="operator_route", target_state="running"
    )._value.get()

    await write_desired_state(
        db_session,
        node=node,
        caller="operator_route",
        write=DesiredStateWrite(target=AppiumDesiredState.running, desired_port=4723),
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_state == AppiumDesiredState.running
    assert node.desired_port == 4723
    assert node.restart_requested_at is None

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


async def test_write_desired_state_stopped_clears_desired_port_and_watermark(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-2", verified=True)
    restart_requested_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=0,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        restart_requested_at=restart_requested_at,
    )
    db_session.add(node)
    await db_session.flush()

    await write_desired_state(
        db_session,
        node=node,
        caller="connectivity",
        write=DesiredStateWrite(target=AppiumDesiredState.stopped),
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_state == AppiumDesiredState.stopped
    assert node.desired_port is None
    assert node.restart_requested_at is None


async def test_write_desired_state_records_restart_watermark(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-3", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=0,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.flush()

    restart_requested_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)

    await write_desired_state(
        db_session,
        node=node,
        caller="operator_restart",
        write=DesiredStateWrite(
            target=AppiumDesiredState.running,
            desired_port=4723,
            restart_requested_at=restart_requested_at,
        ),
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.restart_requested_at == restart_requested_at
    event = (
        (
            await db_session.execute(
                select(DeviceEvent).where(DeviceEvent.device_id == device.id).order_by(DeviceEvent.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert event is not None
    assert event.details is not None
    assert event.details["restart_requested_at"] == restart_requested_at.isoformat()


async def test_write_desired_state_newer_watermark_silently_replaces_old_watermark(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ds-4", verified=True)
    older = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    newer = datetime(2026, 7, 9, 15, 5, tzinfo=UTC)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=0,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        restart_requested_at=older,
    )
    db_session.add(node)
    await db_session.flush()

    await write_desired_state(
        db_session,
        node=node,
        caller="health_restart",
        write=DesiredStateWrite(
            target=AppiumDesiredState.running,
            desired_port=4723,
            restart_requested_at=newer,
        ),
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.restart_requested_at == newer
