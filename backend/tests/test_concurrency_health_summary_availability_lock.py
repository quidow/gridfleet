from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.services import control_plane_state_store, device_health_summary, device_locking
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_health_failure_offline_write_serializes_with_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Health failure offline write does not clobber a concurrent reservation.

    Two outcomes are possible depending on which writer locks first:
    - health_writer first: sets offline → reservation_writer sets reserved → final: reserved
    - reservation_writer first: sets reserved → health_writer sees non-available, skips → final: reserved
    Both paths produce the same final state. The DB lock (SELECT FOR UPDATE) and the
    availability guard in _mark_offline_for_failed_health_signal serialize the writes.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-offline-race",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    async def health_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await device_health_summary.update_device_checks(
                session,
                loaded,
                healthy=False,
                summary="Disconnected",
            )
            await session.commit()

    async def reservation_writer() -> None:
        await asyncio.sleep(0)  # yield to let health_writer's DB query start
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.availability_status = DeviceAvailabilityStatus.reserved
            await session.commit()

    await asyncio.wait_for(asyncio.gather(health_writer(), reservation_writer()), timeout=5.0)

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.availability_status).where(Device.id == device_id))).scalar_one()

    assert final == DeviceAvailabilityStatus.reserved, (
        f"Health failure clobbered concurrent reservation; final availability_status={final.value}"
    )


async def test_health_recovery_available_write_serializes_with_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Health recovery available write does not clobber a concurrent maintenance transition.

    Two outcomes are possible depending on which writer locks first:
    - recovery_writer first: sets available → maintenance_writer sets maintenance → final: maintenance
    - maintenance_writer first: sets maintenance → recovery_writer sees non-offline, skips → final: maintenance
    Both paths produce the same final state. The DB lock and the availability guard in
    _restore_available_for_healthy_signal serialize the writes.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-recovery-race",
        availability_status=DeviceAvailabilityStatus.offline,
        verified=True,
        auto_manage=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            state=NodeState.running,
        )
    )
    await control_plane_state_store.set_value(
        db_session,
        device_health_summary.HEALTH_SUMMARY_NAMESPACE,
        str(device.id),
        {
            "device_checks_healthy": True,
            "session_viability_status": "passed",
            "last_checked_at": datetime.now(UTC).isoformat(),
        },
    )
    await db_session.commit()
    device_id = device.id

    async def recovery_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await session.refresh(loaded, ["appium_node"])
            await device_health_summary.update_node_state(
                session,
                loaded,
                running=True,
                state="running",
            )
            await session.commit()

    async def maintenance_writer() -> None:
        await asyncio.sleep(0)  # yield to let recovery_writer's DB query start
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.availability_status = DeviceAvailabilityStatus.maintenance
            await session.commit()

    await asyncio.wait_for(asyncio.gather(recovery_writer(), maintenance_writer()), timeout=5.0)

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.availability_status).where(Device.id == device_id))).scalar_one()

    assert final == DeviceAvailabilityStatus.maintenance, (
        f"Health recovery clobbered concurrent maintenance; final availability_status={final.value}"
    )


async def test_health_recovery_locks_device_before_summary_patch(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from unittest.mock import patch

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-recovery-lock-order",
        availability_status=DeviceAvailabilityStatus.offline,
        verified=True,
        auto_manage=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            state=NodeState.running,
        )
    )
    await control_plane_state_store.set_value(
        db_session,
        device_health_summary.HEALTH_SUMMARY_NAMESPACE,
        str(device.id),
        {
            "device_checks_healthy": True,
            "session_viability_status": "passed",
            "last_checked_at": datetime.now(UTC).isoformat(),
        },
    )
    await db_session.commit()
    await db_session.refresh(device, ["appium_node"])

    order: list[str] = []
    original_lock_device = device_health_summary._lock_device_for_health_transition
    original_patch_value = device_health_summary.control_plane_state_store.patch_value

    async def record_lock(db: AsyncSession, locked_device: Device | str) -> Device | None:
        order.append("device_lock")
        return await original_lock_device(db, locked_device)

    async def record_patch(
        db: AsyncSession,
        namespace: str,
        key: str,
        value: dict[str, object],
    ) -> None:
        if key == str(device.id) and "node_running" in value:
            order.append("summary_patch")
        await original_patch_value(db, namespace, key, value)

    with (
        patch.object(device_health_summary, "_lock_device_for_health_transition", record_lock),
        patch.object(device_health_summary.control_plane_state_store, "patch_value", record_patch),
    ):
        await device_health_summary.update_node_state(db_session, device, running=True, state="running")

    assert order[:2] == ["device_lock", "summary_patch"]
