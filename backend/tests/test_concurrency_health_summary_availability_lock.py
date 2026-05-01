from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

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
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-offline-race",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    inside_availability_publish = asyncio.Event()
    racer_attempted_lock = asyncio.Event()
    racer_committed = asyncio.Event()

    async def gated_publish(event_name: str, payload: dict[str, object]) -> None:
        if event_name == "device.availability_changed" and payload.get("device_id") == str(device_id):
            inside_availability_publish.set()
            await asyncio.wait_for(racer_attempted_lock.wait(), timeout=2.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(racer_committed.wait(), timeout=0.4)

    async def health_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            with patch("app.services.device_availability.event_bus.publish", gated_publish):
                await device_health_summary.update_device_checks(
                    session,
                    loaded,
                    healthy=False,
                    summary="Disconnected",
                )
            await session.commit()

    async def reservation_writer() -> None:
        await asyncio.wait_for(inside_availability_publish.wait(), timeout=2.0)
        async with db_session_maker() as session:
            racer_attempted_lock.set()
            locked = await device_locking.lock_device(session, device_id)
            locked.availability_status = DeviceAvailabilityStatus.reserved
            await session.commit()
        racer_committed.set()

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

    inside_availability_publish = asyncio.Event()
    racer_attempted_lock = asyncio.Event()
    racer_committed = asyncio.Event()

    async def gated_publish(event_name: str, payload: dict[str, object]) -> None:
        if event_name == "device.availability_changed" and payload.get("device_id") == str(device_id):
            inside_availability_publish.set()
            await asyncio.wait_for(racer_attempted_lock.wait(), timeout=2.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(racer_committed.wait(), timeout=0.4)

    async def recovery_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await session.refresh(loaded, ["appium_node"])
            with patch("app.services.device_availability.event_bus.publish", gated_publish):
                await device_health_summary.update_node_state(
                    session,
                    loaded,
                    running=True,
                    state="running",
                )
            await session.commit()

    async def maintenance_writer() -> None:
        await asyncio.wait_for(inside_availability_publish.wait(), timeout=2.0)
        async with db_session_maker() as session:
            racer_attempted_lock.set()
            locked = await device_locking.lock_device(session, device_id)
            locked.availability_status = DeviceAvailabilityStatus.maintenance
            await session.commit()
        racer_committed.set()

    await asyncio.wait_for(asyncio.gather(recovery_writer(), maintenance_writer()), timeout=5.0)

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.availability_status).where(Device.id == device_id))).scalar_one()

    assert final == DeviceAvailabilityStatus.maintenance, (
        f"Health recovery clobbered concurrent maintenance; final availability_status={final.value}"
    )
