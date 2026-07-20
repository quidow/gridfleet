"""The ``reserved`` dynamic-filter axis must mean one thing everywhere.

``DeviceGroupFacts.is_reserved`` feeds the ``reserved`` filter axis. The grid
allocator populates it from ``reservation_gating_owner_sql`` — the SQL twin of
``reservation_gating_run_id``, which is documented as the single source for
"the allocator's gate and the read-side badge" and therefore excludes
terminal-state runs and effective exclusions.
``load_group_membership_index`` must project the same thing, or a dynamic group
filtered on ``reserved`` routes sessions to devices its own member list does
not show.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.models import Device, DeviceGroup, DeviceReservation, GroupType
from app.devices.services.group_membership import load_group_membership_index
from app.runs.models import RunState, TestRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def _seed_device_reserved_by_run(db_session: AsyncSession, host_id: uuid.UUID, state: RunState) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"res-{uuid.uuid4().hex[:8]}",
        connection_target=f"res-{uuid.uuid4().hex[:8]}",
        name="Reserved Device",
        os_version="14",
        host_id=host_id,
        device_type="real_device",
        connection_type="usb",
    )
    db_session.add(device)
    run = TestRun(
        name=f"run-{uuid.uuid4().hex[:6]}",
        state=state,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    await db_session.flush()
    return device


async def _reserved_axis_members(db_session: AsyncSession, devices: list[Device], *, reserved: bool) -> set[uuid.UUID]:
    group = DeviceGroup(
        key=f"axis-{uuid.uuid4().hex[:6]}",
        name="reserved axis",
        group_type=GroupType.dynamic,
        filters={"reserved": reserved},
    )
    db_session.add(group)
    await db_session.flush()
    index = await load_group_membership_index(
        db_session,
        groups=[group],
        devices=devices,
    )
    return set(index.device_ids(group.key))


async def test_reservation_from_terminated_run_is_not_reserved(db_session: AsyncSession, db_host: Host) -> None:
    """A reservation row owned by a terminated run does not gate the device, so
    the ``reserved`` axis must report it as free — the same answer the grid
    allocator's ``reservation_gating_owner_sql`` projection gives."""
    device = await _seed_device_reserved_by_run(db_session, db_host.id, RunState.completed)

    assert device.id not in await _reserved_axis_members(db_session, [device], reserved=True)
    assert device.id in await _reserved_axis_members(db_session, [device], reserved=False)


async def test_reservation_from_live_run_is_reserved(db_session: AsyncSession, db_host: Host) -> None:
    """Control: an active run's reservation does gate the device, so the axis
    must still report it as reserved."""
    device = await _seed_device_reserved_by_run(db_session, db_host.id, RunState.active)

    assert device.id in await _reserved_axis_members(db_session, [device], reserved=True)
    assert device.id not in await _reserved_axis_members(db_session, [device], reserved=False)
