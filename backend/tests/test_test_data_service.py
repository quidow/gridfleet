import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceTestDataAuditLog
from app.devices.services import test_data as test_data_service
from app.hosts.models import Host
from tests.helpers import create_device_record

pytestmark = pytest.mark.db


async def test_replace_test_data_overwrites_and_logs(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-1",
        name="dev-1",
        test_data={"old": True},
    )
    await db_session.flush()

    result = await test_data_service.replace_device_test_data(db_session, device, {"new": True}, changed_by="op")
    assert result == {"new": True}
    await db_session.refresh(device)
    assert device.test_data == {"new": True}

    logs = (
        (await db_session.execute(select(DeviceTestDataAuditLog).where(DeviceTestDataAuditLog.device_id == device.id)))
        .scalars()
        .all()
    )
    assert len(logs) == 1


async def test_replace_test_data_does_not_clear_verified_at(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-2",
        name="dev-2",
        verified=True,
        test_data={"old": True},
    )
    await db_session.flush()
    assert device.verified_at is not None
    pre = device.verified_at
    await test_data_service.replace_device_test_data(db_session, device, {"new": True}, changed_by="op")
    await db_session.refresh(device)
    assert device.verified_at == pre


async def test_merge_test_data_deep_merges(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-3",
        name="dev-3",
        test_data={"a": {"x": 1}, "b": 2},
    )
    await db_session.flush()

    await test_data_service.merge_device_test_data(db_session, device, {"a": {"y": 9}, "c": 3}, changed_by="op")
    await db_session.refresh(device)
    assert device.test_data == {"a": {"x": 1, "y": 9}, "b": 2, "c": 3}


async def test_get_history_returns_descending(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-tdsvc-4",
        name="dev-4",
    )
    await db_session.flush()

    await test_data_service.replace_device_test_data(db_session, device, {"v": 1}, changed_by="op")
    await test_data_service.replace_device_test_data(db_session, device, {"v": 2}, changed_by="op")
    history = await test_data_service.get_test_data_history(db_session, device.id, limit=10)
    assert [h.new_test_data for h in history] == [{"v": 2}, {"v": 1}]
