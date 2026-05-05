import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.session import Session, SessionStatus
from app.services import device_locking, maintenance_service, session_service
from tests.helpers import create_device

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
            await maintenance_service.enter_maintenance(session, locked, drain=True)

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
    original_set = session_service.set_operational_state

    async def gated_set(
        dev: Device,
        new_status: DeviceOperationalState,
        **kwargs: object,
    ) -> bool:
        if new_status == DeviceOperationalState.busy:
            entered_busy_write.set()
            await asyncio.wait_for(allow_busy_write.wait(), timeout=2.0)
        return await original_set(dev, new_status, **kwargs)

    monkeypatch.setattr(session_service, "set_operational_state", gated_set)

    async def register_running_session() -> None:
        async with db_session_maker() as session:
            await session_service.register_session(
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
        final = (
            await verify.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
        ).one()

    assert final.operational_state == DeviceOperationalState.busy
    assert final.hold == DeviceHold.maintenance


async def test_update_session_status_does_not_overwrite_concurrent_maintenance(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    entered_restore = asyncio.Event()
    allow_restore = asyncio.Event()
    original_restore = session_service.ready_operational_state

    async def gated_restore(db: AsyncSession, dev: Device) -> DeviceOperationalState:
        entered_restore.set()
        await asyncio.wait_for(allow_restore.wait(), timeout=2.0)
        return await original_restore(db, dev)

    monkeypatch.setattr(session_service, "ready_operational_state", gated_restore)

    async def finish_session() -> None:
        async with db_session_maker() as session:
            await session_service.update_session_status(session, "finish-race-session", SessionStatus.passed)

    await asyncio.gather(
        finish_session(),
        _enter_maintenance_after_gate(
            db_session_maker,
            device_id,
            gate=entered_restore,
            release=allow_restore,
        ),
    )

    async with db_session_maker() as verify:
        final = (
            await verify.execute(select(Device.operational_state, Device.hold).where(Device.id == device_id))
        ).one()

    assert final.operational_state == DeviceOperationalState.available
    assert final.hold == DeviceHold.maintenance
