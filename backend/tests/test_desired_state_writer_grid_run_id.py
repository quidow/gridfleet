"""Tests for desired_grid_run_id writer chokepoint."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.desired_state_writer import write_desired_grid_run_id
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceEvent, DeviceOperationalState, DeviceType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_device_with_node(db_session: AsyncSession, host: Host) -> tuple[uuid.UUID, AppiumNode]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="serial",
        identity_scope="host",
        identity_value=f"writer-{uuid.uuid4().hex[:8]}",
        connection_target=f"writer-{uuid.uuid4().hex[:8]}",
        name="Writer Device",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid.example")
    db_session.add(node)
    await db_session.commit()
    return device.id, node


@pytest.mark.db
@pytest.mark.asyncio
async def test_writes_run_id_to_locked_node(db_session: AsyncSession, db_host: Host) -> None:
    device_id, _node = await _seed_device_with_node(db_session, db_host)
    run_id = uuid.uuid4()

    device = await device_locking.lock_device(db_session, device_id, load_sessions=False)
    node = device.appium_node
    assert node is not None

    await write_desired_grid_run_id(
        db_session,
        node=node,
        run_id=run_id,
        caller="run_create",
        actor="tester",
        reason="writer test",
    )
    await db_session.commit()

    refreshed = await db_session.get(AppiumNode, node.id)
    assert refreshed is not None
    assert refreshed.desired_grid_run_id == run_id


@pytest.mark.db
@pytest.mark.asyncio
async def test_clears_run_id_with_none(db_session: AsyncSession, db_host: Host) -> None:
    device_id, _node = await _seed_device_with_node(db_session, db_host)
    run_id = uuid.uuid4()

    device = await device_locking.lock_device(db_session, device_id, load_sessions=False)
    node = device.appium_node
    assert node is not None
    node.desired_grid_run_id = run_id
    await db_session.commit()

    device = await device_locking.lock_device(db_session, device_id, load_sessions=False)
    node = device.appium_node
    assert node is not None
    await write_desired_grid_run_id(
        db_session,
        node=node,
        run_id=None,
        caller="run_complete",
        actor="tester",
    )
    await db_session.commit()

    refreshed = await db_session.get(AppiumNode, node.id)
    assert refreshed is not None
    assert refreshed.desired_grid_run_id is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_no_event_when_value_unchanged(db_session: AsyncSession, db_host: Host) -> None:
    device_id, _node = await _seed_device_with_node(db_session, db_host)
    run_id = uuid.uuid4()

    device = await device_locking.lock_device(db_session, device_id, load_sessions=False)
    node = device.appium_node
    assert node is not None
    node.desired_grid_run_id = run_id
    await db_session.commit()

    device = await device_locking.lock_device(db_session, device_id, load_sessions=False)
    node = device.appium_node
    assert node is not None
    before = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device_id))).scalars().all()

    await write_desired_grid_run_id(
        db_session,
        node=node,
        run_id=run_id,
        caller="run_create",
    )
    await db_session.commit()

    after = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device_id))).scalars().all()
    assert len(after) == len(before)
