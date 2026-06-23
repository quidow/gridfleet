from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

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

    mock_maintenance = MagicMock()
    mock_maintenance.enter_maintenance = fake_enter_maintenance
    mock_maintenance.schedule_device_recovery = MagicMock()

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)

    _settings_enter = FakeSettingsReader()
    async with db_session_maker() as session:
        result = await BulkOperationsService(
            publisher=event_bus,
            settings=_settings_enter,
            circuit_breaker=MagicMock(),
            maintenance=mock_maintenance,
            crud=DeviceCrudService(
                settings=_settings_enter, identity=DeviceIdentityConflictService(), publisher=event_bus
            ),
            operator=OperatorNodeLifecycleService(
                review=build_review_service(), settings=_settings_enter, publisher=event_bus
            ),
        ).bulk_enter_maintenance(session, device_ids)

    assert result == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert lock_device_calls == expected_lock_order
