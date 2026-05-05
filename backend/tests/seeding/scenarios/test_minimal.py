from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.device import Device, DeviceOperationalState
from app.models.device_reservation import DeviceReservation
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.seeding.context import SeedContext
from app.seeding.scenarios.minimal import apply_minimal


@pytest.mark.asyncio
async def test_minimal_scenario_populates_baseline(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_minimal(ctx)
    await db_session.commit()

    hosts = (await db_session.execute(select(Host))).scalars().all()
    devices = (await db_session.execute(select(Device))).scalars().all()
    runs = (await db_session.execute(select(TestRun))).scalars().all()
    sessions = (await db_session.execute(select(Session))).scalars().all()
    reservations = (await db_session.execute(select(DeviceReservation))).scalars().all()

    assert len(hosts) == 1
    assert len(devices) == 2
    assert len(runs) == 2
    assert len(sessions) >= 1
    assert len(reservations) >= 2
    assert any(r.released_at is None for r in reservations)  # active run
    assert any(r.released_at is not None for r in reservations)  # completed run
    active_run_ids = {run.id for run in runs if run.state is RunState.active}
    active_device_ids = {
        session.device_id
        for session in sessions
        if session.run_id in active_run_ids and session.status is SessionStatus.running and session.ended_at is None
    }
    assert active_device_ids
    assert any(device.operational_state is DeviceOperationalState.busy for device in devices)
    assert all(
        device.operational_state is DeviceOperationalState.busy for device in devices if device.id in active_device_ids
    )
