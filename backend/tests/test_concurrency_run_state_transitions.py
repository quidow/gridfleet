import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.models import DeviceHold, DeviceReservation
from app.devices.services.state import DeviceStateService
from app.grid.service import GridService
from app.runs import service as run_service
from app.runs.models import RunState, TestRun
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, create_reserved_run
from tests.helpers import test_event_bus as event_bus

_settings = FakeSettingsReader({})
_grid = GridService(settings=_settings)
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    grid=_grid,
    device_state=DeviceStateService(publisher=event_bus),
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=_release_svc)


@pytest.mark.asyncio
async def test_signal_active_serializes_with_concurrent_cancel(
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    async with db_session_maker() as setup:
        device = await create_device_record(
            setup,
            host_id=default_host_id,
            identity_value="run-transition-race-001",
            name="Run Transition Race 001",
            hold=DeviceHold.reserved,
        )
        run = await create_reserved_run(setup, name="run-transition-race", devices=[device], state=RunState.preparing)
        run_id = run.id

    async def activate() -> str:
        async with db_session_maker() as active_db:
            try:
                await _lifecycle_svc.signal_active(active_db, run_id)
            except ValueError as exc:
                return str(exc)
            return "activated"

    async with db_session_maker() as cancel_db:
        locked_run_result = await cancel_db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
        locked_run = locked_run_result.scalar_one()
        assert locked_run.state == RunState.preparing

        active_task = asyncio.create_task(activate())
        await asyncio.sleep(0.15)
        assert not active_task.done()

        await _lifecycle_svc.cancel_run(cancel_db, run_id)
        active_result = await asyncio.wait_for(active_task, timeout=5.0)

    assert "Cannot signal active from state 'cancelled'" in active_result

    async with db_session_maker() as verify_db:
        final_run = await run_service.get_run(verify_db, run_id)
        assert final_run is not None
        assert final_run.state == RunState.cancelled

        reservations = (
            (await verify_db.execute(select(DeviceReservation).where(DeviceReservation.run_id == run_id)))
            .scalars()
            .all()
        )
        assert reservations
        assert all(reservation.released_at is not None for reservation in reservations)
