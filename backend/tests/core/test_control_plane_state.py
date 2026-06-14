import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.state import set_operational_state
from app.hosts.models import Host
from app.jobs.models import Job
from app.sessions.models import Session, SessionStatus
from app.verification.services.service import VerificationService
from tests.helpers import (
    drain_handlers,
    get_connectivity_control_plane_state,
    get_session_viability_control_plane_state,
    recent_events,
    reset_connectivity_control_plane_state,
    reset_session_viability_control_plane_state,
    set_session_viability_control_plane_entry,
    store_verification_job_for_test,
    track_previously_offline_device,
)
from tests.helpers import test_event_bus as event_bus


async def test_control_plane_state_helpers_snapshot_and_reset(db_session: AsyncSession, db_host: Host) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    job_id = "00000000-0000-0000-0000-000000000001"
    await store_verification_job_for_test(
        job_id,
        {
            "job_id": job_id,
            "status": "running",
        },
        session_factory=session_factory,
    )
    device_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    with state_write_guard.bypass():
        device = Device(
            id=device_id,
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="control-plane-device",
            connection_target="control-plane-device",
            name="Control Plane Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    session = Session(session_id="sess-1", device_id=device_id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    await track_previously_offline_device(db_session, "device-1")
    await set_session_viability_control_plane_entry(
        db_session,
        "device-1",
        {
            "status": "passed",
            "last_attempted_at": "2026-03-31T00:00:00+00:00",
            "last_succeeded_at": "2026-03-31T00:00:00+00:00",
            "error": None,
            "checked_by": "scheduled",
        },
    )

    persisted_job = await db_session.get(Job, uuid.UUID(job_id))
    assert persisted_job is not None
    running_sessions = await db_session.execute(select(Session).where(Session.status == SessionStatus.running))
    assert {session.session_id: str(session.device_id) for session in running_sessions.scalars().all()} == {
        "sess-1": str(device_id)
    }
    assert await get_connectivity_control_plane_state(db_session) == {"device-1"}
    assert "device-1" in (await get_session_viability_control_plane_state(db_session))["state"]

    await VerificationService().clear_verification_jobs(session_factory=session_factory)
    await reset_connectivity_control_plane_state(db_session)
    await reset_session_viability_control_plane_state(db_session)

    async with session_factory() as fresh_db:
        assert await fresh_db.get(Job, uuid.UUID(job_id)) is None
    assert await get_connectivity_control_plane_state(db_session) == set()
    assert (await get_session_viability_control_plane_state(db_session))["state"] == {}


async def test_set_operational_state_publishes_only_on_change(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="availability-status-1",
            connection_target="availability-status-1",
            name="Availability Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    changed = await set_operational_state(device, DeviceOperationalState.available, publisher=event_bus)
    assert changed is False
    assert recent_events(event_bus) == []

    changed = await set_operational_state(
        device, DeviceOperationalState.busy, reason="Probe started", publisher=event_bus
    )
    assert changed is True
    await db_session.commit()
    await drain_handlers(event_bus)
    events = recent_events(event_bus)
    assert len(events) == 1
    assert events[0]["data"]["old_operational_state"] == "available"
    assert events[0]["data"]["new_operational_state"] == "busy"
    assert events[0]["data"]["reason"] == "Probe started"
