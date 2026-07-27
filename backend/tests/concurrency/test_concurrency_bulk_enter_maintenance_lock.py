import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.devices import locking as device_locking
from app.devices.models import DeviceOperationalState
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

    from app.devices.locking import LockedDevice
    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_enter_maintenance_holds_one_device_lock_per_item_transaction(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Each item takes its own ``FOR UPDATE`` and holds it for its own transaction.

    Before the per-item split, every device was locked up front on one shared
    session, so any intermediate commit released the whole batch. The regression
    that matters is the opposite direction too: an item that is still in flight
    must genuinely hold its row against a peer session.
    """
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
    locked_ids: list[uuid.UUID] = []
    held = asyncio.Event()
    release = asyncio.Event()
    racer_acquired = asyncio.Event()

    class GatedMaintenance:
        async def enter_maintenance_locked(self, db: AsyncSession, locked: LockedDevice, **_kwargs: object) -> None:
            locked.assert_active(db)
            locked_ids.append(locked.device.id)
            if locked.device.id == first.id:
                held.set()
                await asyncio.wait_for(release.wait(), timeout=3.0)

    async def racer() -> None:
        await asyncio.wait_for(held.wait(), timeout=3.0)
        async with db_session_maker() as racer_db:
            try:
                await asyncio.wait_for(device_locking.lock_device(racer_db, first.id), timeout=0.3)
                racer_acquired.set()
            except TimeoutError:
                pass
            finally:
                await racer_db.rollback()
        release.set()

    _settings_enter = FakeSettingsReader()
    service = BulkOperationsService(
        publisher=event_bus,
        settings=_settings_enter,
        circuit_breaker=MagicMock(),
        maintenance=GatedMaintenance(),  # type: ignore[arg-type]
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_enter, publisher=event_bus
        ),
        session_factory=db_session_maker,
    )
    result, _ = await asyncio.gather(service.bulk_enter_maintenance(device_ids), racer())

    assert result == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert sorted(locked_ids) == sorted(device_ids), f"each device must be locked exactly once, got {locked_ids}"
    assert not racer_acquired.is_set(), (
        "a peer session acquired the first device's row while its item transaction "
        "was still open — the per-item FOR UPDATE is not being held"
    )
