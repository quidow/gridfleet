import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, NoResultFound

from app.devices import locking as device_locking
from app.devices.models import DeviceTestDataAuditLog
from app.devices.services.test_data import TestDataService
from app.events.models import SystemEvent
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice
    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def test_replace_test_data_overwrites_and_logs(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-1",
        name="dev-1",
        test_data={"old": True},
    )

    svc = TestDataService(publisher=event_bus)
    async with db_session_maker() as command_db, command_db.begin():
        result = await svc.replace_device_test_data(command_db, device.id, {"new": True}, changed_by="op")
    assert result == {"new": True}
    await db_session.refresh(device)
    assert device.test_data == {"new": True}

    logs = (
        (await db_session.execute(select(DeviceTestDataAuditLog).where(DeviceTestDataAuditLog.device_id == device.id)))
        .scalars()
        .all()
    )
    assert len(logs) == 1


async def test_replace_test_data_does_not_clear_verified_at(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-2",
        name="dev-2",
        verified=True,
        test_data={"old": True},
    )
    assert device.verified_at is not None
    pre = device.verified_at
    svc = TestDataService(publisher=event_bus)
    async with db_session_maker() as command_db, command_db.begin():
        await svc.replace_device_test_data(command_db, device.id, {"new": True}, changed_by="op")
    await db_session.refresh(device)
    assert device.verified_at == pre


async def test_merge_test_data_deep_merges(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-3",
        name="dev-3",
        test_data={"a": {"x": 1}, "b": 2},
    )

    svc = TestDataService(publisher=event_bus)
    async with db_session_maker() as command_db, command_db.begin():
        await svc.merge_device_test_data(command_db, device.id, {"a": {"y": 9}, "c": 3}, changed_by="op")
    await db_session.refresh(device)
    assert device.test_data == {"a": {"x": 1, "y": 9}, "b": 2, "c": 3}


async def test_get_history_returns_descending(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-4",
        name="dev-4",
    )

    svc = TestDataService(publisher=event_bus)
    # One transaction per write: ``changed_at`` server-defaults to the
    # transaction timestamp, so two writes sharing a transaction would tie.
    async with db_session_maker() as command_db, command_db.begin():
        await svc.replace_device_test_data(command_db, device.id, {"v": 1}, changed_by="op")
    async with db_session_maker() as command_db, command_db.begin():
        await svc.replace_device_test_data(command_db, device.id, {"v": 2}, changed_by="op")
    history = await svc.get_test_data_history(db_session, device.id, limit=10)
    assert [h.new_test_data for h in history] == [{"v": 2}, {"v": 1}]


async def test_merge_test_data_locks_the_device_exactly_once(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merge reads the current payload through the same proof it writes under."""
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-lock-1",
        name="dev-lock-1",
        test_data={"a": {"x": 1}},
    )
    proofs: list[uuid.UUID] = []
    real_lock = device_locking.lock_device_handle

    async def spy(db: AsyncSession, device_id: uuid.UUID, **kwargs: bool) -> LockedDevice:
        locked = await real_lock(db, device_id, **kwargs)
        locked.assert_active(db)
        proofs.append(locked.device.id)
        return locked

    monkeypatch.setattr(device_locking, "lock_device_handle", spy)

    svc = TestDataService(publisher=event_bus)
    async with db_session_maker() as command_db, command_db.begin():
        await svc.merge_device_test_data(command_db, device.id, {"a": {"y": 2}}, changed_by="op")

    assert proofs == [device.id], "the merge command must take exactly one Device aggregate lock"
    await db_session.refresh(device)
    assert device.test_data == {"a": {"x": 1, "y": 2}}


async def test_replace_test_data_rejects_a_missing_device(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    svc = TestDataService(publisher=event_bus)
    with pytest.raises(NoResultFound):
        async with db_session_maker() as command_db, command_db.begin():
            await svc.replace_device_test_data(command_db, uuid.uuid4(), {"v": 1})


async def test_replace_test_data_rolls_back_mutation_audit_row_and_event(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-rollback",
        name="dev-rollback",
        test_data={"keep": True},
    )
    svc = TestDataService(publisher=event_bus)

    with pytest.raises(DBAPIError):
        async with db_session_maker() as command_db, command_db.begin():
            await svc.replace_device_test_data(command_db, device.id, {"gone": True}, changed_by="op")
            # A real aborting statement, not a patched side effect: the mutation,
            # the audit row, and the outbox row must die as one unit.
            await command_db.execute(text("SELECT 1 / 0"))

    await db_session.refresh(device)
    assert device.test_data == {"keep": True}, "the JSON mutation outlived its rolled-back transaction"
    audit_rows = await db_session.scalar(
        select(func.count()).select_from(DeviceTestDataAuditLog).where(DeviceTestDataAuditLog.device_id == device.id)
    )
    assert audit_rows == 0, "audit row outlived its rolled-back transaction"
    events = await db_session.scalar(
        select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "test_data.updated")
    )
    assert events == 0, "outbox row outlived its rolled-back transaction"
