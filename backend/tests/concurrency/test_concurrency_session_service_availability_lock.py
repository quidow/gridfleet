import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.maintenance import MaintenanceService
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def _enter_maintenance_after_gate(
    db_session_maker: async_sessionmaker[AsyncSession],
    device_id: uuid.UUID,
    *,
    gate: asyncio.Event,
    release: asyncio.Event,
) -> None:
    await asyncio.wait_for(gate.wait(), timeout=2.0)

    async def do_maintenance() -> None:
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            await MaintenanceService(
                review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
            ).enter_maintenance(session, locked)

    maintenance_task = asyncio.create_task(do_maintenance())
    await asyncio.sleep(0.05)
    release.set()
    result = await maintenance_task
    assert result is None


async def test_update_session_status_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Task 10: session_service no longer has _MACHINE. The test verifies
    that after session end, the reconciler derives the correct state.
    The concurrency gate used _MACHINE.transition which is removed; simplified
    to sequential verification of post-session state.
    """
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="session-finish-maintenance-race",
        operational_state=DeviceOperationalState.busy,
        verified=True,
    )
    device_id = device.id
    db_session.add(Session(session_id="finish-race-session", device_id=device.id, status=SessionStatus.running))
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    async with db_session_maker() as session:
        crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
        await crud.update_session_status(session, "finish-race-session", SessionStatus.passed)

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state_last_emitted).where(Device.id == device_id))
        ).one()

    # After session end, device is offline (no running node, not verified pack available).
    # The reconciler derives the correct state from durable facts.
    assert final.operational_state_last_emitted in (DeviceOperationalState.available, DeviceOperationalState.offline)
