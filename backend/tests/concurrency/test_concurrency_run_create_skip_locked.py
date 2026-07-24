import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.runs import service_allocator
from app.runs.schemas import DeviceRequirement, RunCreate
from app.runs.service_allocator import RunAllocatorService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_settings = FakeSettingsReader({})

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_create_run_retries_when_candidate_row_transiently_locked(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device whose row is momentarily locked by a concurrent transaction is
    dropped by the batch allocator's locked recheck
    (``SELECT ... FOR UPDATE OF devices SKIP LOCKED``), yielding a spurious
    "not enough devices". ``create_run`` must re-match once the transient lock
    clears instead of surfacing the false negative.

    The wrapper counts and gates ``_batch_select_devices`` but delegates to the
    real implementation, so the SKIP LOCKED drop under contention is produced by
    production code, not by the test double.
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

    real_batch_select = service_allocator._batch_select_devices
    calls = 0

    async def counting_batch_select(
        db: AsyncSession,
        requirements: list[DeviceRequirement],
        *,
        restart_window_sec: int,
    ) -> list[list[Device]]:
        nonlocal calls
        calls += 1
        result = await real_batch_select(
            db,
            requirements,
            restart_window_sec=restart_window_sec,
        )
        if calls == 1:
            # First pass races the held lock: SKIP LOCKED drops the only device.
            assert result == [[]]
            first_match_done.set()
            await asyncio.wait_for(lock_released.wait(), timeout=5.0)
        return result

    monkeypatch.setattr(service_allocator, "_batch_select_devices", counting_batch_select)

    allocator_svc = RunAllocatorService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=test_circuit_breaker,
        session_factory=db_session_maker,
    )

    async def hold_then_release() -> None:
        async with db_session_maker() as session:
            await device_locking.lock_device(session, device_id)  # SELECT ... FOR UPDATE
            lock_acquired.set()
            await asyncio.wait_for(first_match_done.wait(), timeout=5.0)
            await session.commit()  # releases the row lock
        lock_released.set()

    async def do_create() -> list[str]:
        await asyncio.wait_for(lock_acquired.wait(), timeout=5.0)
        result = await allocator_svc.create_run(
            RunCreate(
                name="skip-locked-run",
                requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
            ),
        )
        return [info.device_id for info in result.response.devices]

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

    async def always_empty(
        db: AsyncSession,
        requirements: list[DeviceRequirement],
        *,
        restart_window_sec: int,
    ) -> list[list[Device]]:
        nonlocal calls
        _ = db, restart_window_sec
        calls += 1
        return [[] for _ in requirements]

    monkeypatch.setattr(service_allocator, "_batch_select_devices", always_empty)
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)  # keep the test fast

    allocator_svc = RunAllocatorService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=test_circuit_breaker,
        session_factory=db_session_maker,
    )
    with pytest.raises(ValueError, match="Not enough devices"):
        await allocator_svc.create_run(
            RunCreate(
                name="retry-budget-run",
                requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
            ),
        )

    assert calls == 5
