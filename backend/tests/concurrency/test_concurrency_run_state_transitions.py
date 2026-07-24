from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.devices.models import DeviceReservation
from app.runs import service as run_service
from app.runs.models import RunState
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_settings = FakeSettingsReader({})


def _make_lifecycle(session_factory: async_sessionmaker[AsyncSession]) -> RunLifecycleService:
    release = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    return RunLifecycleService(
        publisher=event_bus, settings=_settings, release=release, session_factory=session_factory
    )


@pytest.mark.asyncio
async def test_signal_active_serializes_with_concurrent_cancel(
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    """signal_active and cancel_run each own their own transaction and lock the
    run FOR UPDATE, so a concurrent pair serialises at the DB. Whichever wins,
    the run ends ``cancelled`` and every reservation is released — never a torn
    or half-active state."""
    async with db_session_maker() as setup:
        device = await create_device_record(
            setup,
            host_id=default_host_id,
            identity_value="run-transition-race-001",
            name="Run Transition Race 001",
        )
        run = await create_reserved_run(setup, name="run-transition-race", devices=[device], state=RunState.preparing)
        run_id = run.id

    lifecycle = _make_lifecycle(db_session_maker)

    async def activate() -> str:
        try:
            await lifecycle.signal_active(run_id)
        except ValueError as exc:
            return str(exc)
        return "activated"

    async def cancel() -> None:
        await lifecycle.cancel_run(run_id)

    active_result, _ = await asyncio.gather(activate(), cancel())

    assert active_result == "activated" or "Cannot signal active" in active_result

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
