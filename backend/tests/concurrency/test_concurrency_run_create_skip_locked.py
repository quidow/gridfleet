import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.runs import service_allocator
from app.runs.schemas import RunCreate
from app.runs.service_allocator import RunAllocatorService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_settings = FakeSettingsReader({})
_allocator_svc = RunAllocatorService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=test_circuit_breaker,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_create_run_retries_when_candidate_row_transiently_locked(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device whose row is momentarily locked by a concurrent transaction is
    dropped by the allocator's Stage-3 ``SELECT ... FOR UPDATE SKIP LOCKED``,
    yielding a spurious "not enough devices". The allocator must re-match once
    the transient lock clears instead of surfacing the false negative.
    """
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="skip-locked",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_id = device.id
    await db_session.commit()

    lock_acquired = asyncio.Event()
    first_match_done = asyncio.Event()
    lock_released = asyncio.Event()

    real_find = service_allocator._find_matching_devices
    calls = 0

    async def counting_find(db: AsyncSession, requirement: object, excluded_device_ids: object = None) -> list[Device]:
        nonlocal calls
        calls += 1
        result = await real_find(db, requirement, excluded_device_ids=excluded_device_ids)  # type: ignore[arg-type]
        if calls == 1:
            # First pass races the held lock: SKIP LOCKED drops the only device.
            assert result == []
            first_match_done.set()
            await asyncio.wait_for(lock_released.wait(), timeout=5.0)
        return result

    monkeypatch.setattr(service_allocator, "_find_matching_devices", counting_find)

    async def hold_then_release() -> None:
        async with db_session_maker() as session:
            await device_locking.lock_device(session, device_id)  # SELECT ... FOR UPDATE
            lock_acquired.set()
            await asyncio.wait_for(first_match_done.wait(), timeout=5.0)
            await session.commit()  # releases the row lock
        lock_released.set()

    async def do_create() -> list[str]:
        await asyncio.wait_for(lock_acquired.wait(), timeout=5.0)
        async with db_session_maker() as session:
            _, device_infos = await _allocator_svc.create_run(
                session,
                RunCreate(
                    name="skip-locked-run",
                    requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
                ),
            )
            return [info.device_id for info in device_infos]

    reserved_ids, _ = await asyncio.gather(do_create(), hold_then_release())

    assert reserved_ids == [str(device_id)]
    assert calls == 2  # one skipped pass, one successful re-match

    async with db_session_maker() as verify:
        reservation = (
            await verify.execute(
                select(DeviceReservation).where(
                    DeviceReservation.device_id == device_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    assert reservation is not None


async def test_create_run_exhausts_widened_retry_budget(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-match budget must be 5 attempts (lab-measured loop lock holds had a
    ~600ms median; the old 3x50ms budget could not bridge them). Pins the
    attempt count on the exhaustion path."""
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="retry-budget",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    calls = 0

    async def always_empty(db: AsyncSession, requirement: object, excluded_device_ids: object = None) -> list[Device]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(service_allocator, "_find_matching_devices", always_empty)
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)  # keep the test fast

    async with db_session_maker() as session:
        with pytest.raises(ValueError, match="Not enough devices"):
            await _allocator_svc.create_run(
                session,
                RunCreate(
                    name="retry-budget-run",
                    requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
                ),
            )

    assert calls == 5
