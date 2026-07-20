"""A locked candidate must not shorten a requirement that has spares available.

``_batch_select_devices`` selects candidates before the ``SELECT ... FOR UPDATE
... SKIP LOCKED`` recheck. If it carried exactly ``req.count`` picks into the
lock, one row held by a concurrent transaction left the requirement short with
no fallback — and because selection is deterministic (oldest-first over the same
candidate list) the re-match loop re-picks the same devices every attempt, so a
lock held for the whole create surfaces "Not enough devices" while eligible
devices sit free.
"""

import asyncio
from typing import TYPE_CHECKING

import pytest

from app.devices import locking as device_locking
from app.devices.models import DeviceOperationalState
from app.runs.schemas import RunCreate
from app.runs.service_allocator import RunAllocatorService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_allocator_svc = RunAllocatorService(
    publisher=event_bus,
    settings=FakeSettingsReader({}),
    circuit_breaker=test_circuit_breaker,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_create_run_falls_back_to_spare_when_first_pick_is_locked(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Five eligible devices, ``count=2``, a competing transaction holding the row
    lock on the oldest for the whole create. The run must still be created against
    two of the four free devices instead of raising "Not enough devices"."""
    devices = []
    for index in range(5):
        devices.append(
            await create_device(
                db_session,
                host_id=default_host_id,
                name=f"spare-{index}",
                operational_state=DeviceOperationalState.available,
                verified=True,
            )
        )
        # Distinct created_at ordering is what makes the pre-lock pick deterministic.
        await db_session.commit()
    locked_id = devices[0].id
    pack_id, platform_id = devices[0].pack_id, devices[0].platform_id

    lock_acquired = asyncio.Event()
    create_done = asyncio.Event()

    async def hold_lock() -> None:
        async with db_session_maker() as session:
            await device_locking.lock_device(session, locked_id)  # SELECT ... FOR UPDATE
            lock_acquired.set()
            await asyncio.wait_for(create_done.wait(), timeout=30.0)
            await session.rollback()

    async def do_create() -> list[str]:
        await asyncio.wait_for(lock_acquired.wait(), timeout=5.0)
        try:
            async with db_session_maker() as session:
                _, device_infos = await _allocator_svc.create_run(
                    session,
                    RunCreate(
                        name="spare-fallback-run",
                        requirements=[{"pack_id": pack_id, "platform_id": platform_id, "count": 2}],
                    ),
                )
                return [info.device_id for info in device_infos]
        finally:
            create_done.set()

    reserved_ids, _ = await asyncio.gather(do_create(), hold_lock())

    assert len(reserved_ids) == 2
    assert str(locked_id) not in reserved_ids
