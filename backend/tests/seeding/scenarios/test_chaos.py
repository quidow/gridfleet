# backend/tests/seeding/scenarios/test_chaos.py
import pytest
from sqlalchemy import select

from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host, HostStatus
from app.models.job import Job
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.seeding.context import SeedContext
from app.seeding.scenarios.chaos import apply_chaos


@pytest.mark.asyncio
async def test_chaos_scenario_emits_every_error_signal(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_chaos(ctx)
    await db_session.commit()

    hosts = (await db_session.execute(select(Host))).scalars().all()
    assert any(h.status is HostStatus.offline for h in hosts)

    devices = (await db_session.execute(select(Device))).scalars().all()
    assert any(d.hold is DeviceHold.maintenance for d in devices)

    events = (await db_session.execute(select(DeviceEvent))).scalars().all()
    assert any(e.event_type is DeviceEventType.connectivity_lost for e in events)
    assert any(e.event_type is DeviceEventType.connectivity_restored for e in events)

    stuck = [
        s
        for s in (await db_session.execute(select(Session))).scalars().all()
        if s.status is SessionStatus.running and s.ended_at is None
    ]
    assert len(stuck) >= 1
    active_run_ids = {
        run.id for run in (await db_session.execute(select(TestRun))).scalars().all() if run.state is RunState.active
    }
    assert active_run_ids
    active_running_device_ids = {session.device_id for session in stuck if session.run_id in active_run_ids}
    assert active_running_device_ids
    assert any(d.operational_state is DeviceOperationalState.busy for d in devices)
    assert all(
        device.operational_state is DeviceOperationalState.busy
        for device in devices
        if device.id in active_running_device_ids
    )

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert any(j.status == "failed" for j in jobs)
