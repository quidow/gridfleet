import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import DeviceAvailabilityStatus
from app.models.device_reservation import DeviceReservation
from app.models.test_run import RunState, TestRun
from app.services import run_service
from tests.helpers import create_device_record, create_reserved_run


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
            availability_status=DeviceAvailabilityStatus.reserved,
        )
        run = await create_reserved_run(setup, name="run-transition-race", devices=[device], state=RunState.ready)
        run_id = run.id

    async def activate() -> str:
        async with db_session_maker() as active_db:
            try:
                await run_service.signal_active(active_db, run_id)
            except ValueError as exc:
                return str(exc)
            return "activated"

    async with db_session_maker() as cancel_db:
        locked_run_result = await cancel_db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
        locked_run = locked_run_result.scalar_one()
        assert locked_run.state == RunState.ready

        active_task = asyncio.create_task(activate())
        await asyncio.sleep(0.15)
        assert not active_task.done()

        await run_service.cancel_run(cancel_db, run_id)
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
