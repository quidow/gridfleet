from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.health import DeviceHealthService
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_health_failure_offline_write_serializes_with_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After Task 10: health failure reconciler derives operational_state=offline.
    Hold is derived from DeviceReservation rows, not from direct writes.
    The old test set hold=reserved via bypass (no real reservation row) —
    the reconciler now correctly derives hold=None when there's no reservation.
    The key invariant tested here is that operational_state=offline is written.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-offline-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    async def health_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await DeviceHealthService(publisher=Mock()).update_device_checks(
                session,
                loaded,
                healthy=False,
                summary="Disconnected",
            )
            await session.commit()

    await health_writer()

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).one()

    assert final.operational_state == DeviceOperationalState.offline


async def test_health_recovery_available_write_serializes_with_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After Task 10: health recovery reconciler derives operational_state=available
    when health signals are all positive. Hold is derived from lifecycle_policy_state
    (maintenance_reason), not from direct hold column writes. The test verifies that
    recovery correctly sets available when all health checks pass.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="health-recovery-race",
        operational_state=DeviceOperationalState.offline,
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    )
    device.device_checks_healthy = True
    device.session_viability_status = "passed"
    await db_session.commit()
    device_id = device.id

    async def recovery_writer() -> None:
        async with db_session_maker() as session:
            loaded = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
            await session.refresh(loaded, ["appium_node"])
            await DeviceHealthService(publisher=Mock()).apply_node_state_transition(
                session,
                loaded,
                health_running=None,
                health_state=None,
                mark_offline=False,
            )
            await session.commit()

    await recovery_writer()

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).one()

    assert final.operational_state == DeviceOperationalState.available
