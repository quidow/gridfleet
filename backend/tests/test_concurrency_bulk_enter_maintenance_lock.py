import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services import bulk_service, device_locking
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_enter_maintenance_relocks_each_device_before_enter_after_intermediate_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-relock-a",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    second = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-relock-b",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    device_ids = [first.id, second.id]
    expected_lock_order = sorted(device_ids)
    original_lock_device = device_locking.lock_device
    lock_device_calls: list[uuid.UUID] = []
    first_enter = True

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        locked = await original_lock_device(db, target_id, load_sessions=load_sessions)
        if target_id in device_ids:
            lock_device_calls.append(target_id)
        return locked

    async def fake_enter_maintenance(
        db: AsyncSession,
        device: Device,
        *,
        commit: bool = True,
        allow_reserved: bool = False,
    ) -> Device:
        nonlocal first_enter
        _ = allow_reserved
        assert commit is False
        if first_enter:
            first_enter = False
            await db.commit()
        return device

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)
    monkeypatch.setattr(bulk_service, "enter_maintenance", fake_enter_maintenance)

    async with db_session_maker() as session:
        result = await bulk_service.bulk_enter_maintenance(session, device_ids)

    assert result == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert lock_device_calls == expected_lock_order
