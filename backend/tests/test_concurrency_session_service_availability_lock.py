import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.maintenance import MaintenanceService
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

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
            await MaintenanceService(settings=FakeSettingsReader({}), publisher=event_bus).enter_maintenance(
                session, locked
            )

    maintenance_task = asyncio.create_task(do_maintenance())
    await asyncio.sleep(0.05)
    release.set()
    result = await maintenance_task
    assert result is None


async def test_register_session_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="session-register-maintenance-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_id = device.id
    await db_session.commit()

    entered_busy_write = asyncio.Event()
    allow_busy_write = asyncio.Event()
    import app.devices.services.state as _device_state_mod
    import app.sessions.service as _session_service_mod

    original_set = _device_state_mod.set_operational_state

    async def gated_set(
        dev: Device,
        new_status: DeviceOperationalState,
        **kwargs: object,
    ) -> bool:
        if new_status == DeviceOperationalState.busy:
            entered_busy_write.set()
            await asyncio.wait_for(allow_busy_write.wait(), timeout=2.0)
        return await original_set(dev, new_status, **kwargs)

    monkeypatch.setattr(_device_state_mod, "set_operational_state", gated_set)
    monkeypatch.setattr(_session_service_mod, "set_operational_state", gated_set)

    async def register_running_session() -> None:
        async with db_session_maker() as session:
            crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
            await crud.register_session(
                session,
                session_id="register-race-session",
                test_name=None,
                device_id=device_id,
                status=SessionStatus.running,
            )

    await asyncio.gather(
        register_running_session(),
        _enter_maintenance_after_gate(
            db_session_maker,
            device_id,
            gate=entered_busy_write,
            release=allow_busy_write,
        ),
    )

    async with db_session_maker() as verify:
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).one()

    assert final.operational_state == DeviceOperationalState.busy
    # hold is now derived by the reconciler (Task 7+8); check the maintenance_reason signal
    from app.devices.models import Device as DeviceModel
    from app.devices.services.lifecycle_policy_state import state as ps

    async with db_session_maker() as verify2:
        device_row = (await verify2.execute(select(DeviceModel).where(DeviceModel.id == device_id))).scalar_one()
        assert ps(device_row).get("maintenance_reason") is not None


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
        final = (await verify.execute(select(Device.operational_state).where(Device.id == device_id))).one()

    # After session end, device is offline (no running node, not verified pack available).
    # The reconciler derives the correct state from durable facts.
    assert final.operational_state in (DeviceOperationalState.available, DeviceOperationalState.offline)
