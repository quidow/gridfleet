import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.analytics_capacity_snapshot import AnalyticsCapacitySnapshot
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host
from app.models.host_resource_sample import HostResourceSample
from app.models.session import Session, SessionStatus
from app.services.data_cleanup import _cleanup_old_data
from app.services.event_bus import event_bus
from app.settings.models import ConfigAuditLog


async def _create_device(db: AsyncSession, host: Host) -> uuid.UUID:
    """Create a minimal device for FK references."""
    from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType

    connection_target = f"test-{uuid.uuid4().hex[:8]}"
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=connection_target,
        connection_target=connection_target,
        name="test-device",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db.add(device)
    await db.flush()
    return device.id


async def test_cleanup_old_sessions(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)

    # Old completed session
    old_session = Session(
        session_id="old-session",
        device_id=device_id,
        status=SessionStatus.passed,
        started_at=old_time,
        ended_at=old_time + timedelta(hours=1),
    )
    db_session.add(old_session)

    # Recent session
    recent_session = Session(
        session_id="recent-session",
        device_id=device_id,
        status=SessionStatus.passed,
        started_at=datetime.now(UTC) - timedelta(days=1),
        ended_at=datetime.now(UTC),
    )
    db_session.add(recent_session)

    # Running session (should not be deleted regardless of age)
    running_session = Session(
        session_id="running-session",
        device_id=device_id,
        status=SessionStatus.running,
        started_at=old_time,
    )
    db_session.add(running_session)
    await db_session.commit()

    await _cleanup_old_data(db_session)

    result = await db_session.execute(select(Session))
    remaining = result.scalars().all()
    session_ids = {s.session_id for s in remaining}
    assert "old-session" not in session_ids
    assert "recent-session" in session_ids
    assert "running-session" in session_ids


async def test_cleanup_old_agent_reconfigure_outbox_rows(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=10)
    recent_time = datetime.now(UTC) - timedelta(hours=1)
    old_delivered = AgentReconfigureOutbox(
        device_id=device_id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        created_at=old_time,
        delivered_at=old_time,
    )
    old_abandoned = AgentReconfigureOutbox(
        device_id=device_id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=2,
        created_at=old_time,
        abandoned_at=old_time,
        abandoned_reason="max delivery attempts exceeded",
    )
    old_pending = AgentReconfigureOutbox(
        device_id=device_id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=3,
        created_at=old_time,
    )
    recent_delivered = AgentReconfigureOutbox(
        device_id=device_id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=4,
        created_at=recent_time,
        delivered_at=recent_time,
    )
    db_session.add_all([old_delivered, old_abandoned, old_pending, recent_delivered])
    await db_session.commit()

    await _cleanup_old_data(db_session)

    remaining = (await db_session.execute(select(AgentReconfigureOutbox))).scalars().all()
    remaining_ids = {row.id for row in remaining}
    assert old_delivered.id not in remaining_ids
    assert old_abandoned.id not in remaining_ids
    assert old_pending.id in remaining_ids
    assert recent_delivered.id in remaining_ids


async def test_cleanup_old_audit_logs(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=200)

    old_log = ConfigAuditLog(
        device_id=device_id,
        new_config={"key": "value"},
        changed_at=old_time,
    )
    db_session.add(old_log)

    recent_log = ConfigAuditLog(
        device_id=device_id,
        new_config={"key": "value2"},
        changed_at=datetime.now(UTC),
    )
    db_session.add(recent_log)
    await db_session.commit()

    await _cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(ConfigAuditLog))
    remaining = result.scalars().all()
    assert len(remaining) == 1
    assert remaining[0].new_config == {"key": "value2"}


async def test_cleanup_old_device_events(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)

    old_event = DeviceEvent(
        device_id=device_id,
        event_type=DeviceEventType.connectivity_lost,
        details={"reason": "test"},
        created_at=old_time,
    )
    db_session.add(old_event)

    recent_event = DeviceEvent(
        device_id=device_id,
        event_type=DeviceEventType.connectivity_restored,
        details={"reason": "test"},
        created_at=datetime.now(UTC),
    )
    db_session.add(recent_event)
    await db_session.commit()

    await _cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(DeviceEvent))
    remaining = result.scalars().all()
    assert len(remaining) == 1
    assert remaining[0].event_type == DeviceEventType.connectivity_restored


async def test_cleanup_batches_deletes_and_reports_aggregated_counts(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)
    db_session.add_all(
        [
            Session(
                session_id=f"old-session-{index}",
                device_id=device_id,
                status=SessionStatus.passed,
                started_at=old_time - timedelta(minutes=index),
                ended_at=old_time - timedelta(minutes=index - 1),
            )
            for index in range(5)
        ]
    )
    await db_session.commit()

    event_bus.reset()
    with (
        patch("app.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await _cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(Session))
    remaining = result.scalars().all()
    assert len(remaining) == 1
    events = event_bus.get_recent_events(event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["sessions_deleted"] == 4
    assert events[0]["data"]["host_resource_samples_deleted"] == 0


async def test_cleanup_host_resource_samples_in_batches_and_reports_counts(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    old_time = datetime.now(UTC) - timedelta(days=2)
    recent_time = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all(
        [
            HostResourceSample(
                host_id=db_host.id,
                recorded_at=old_time - timedelta(minutes=index),
                cpu_percent=10.0 + index,
                memory_used_mb=1000,
                memory_total_mb=2000,
                disk_used_gb=10.0,
                disk_total_gb=20.0,
                disk_percent=50.0,
            )
            for index in range(5)
        ]
        + [
            HostResourceSample(
                host_id=db_host.id,
                recorded_at=recent_time,
                cpu_percent=20.0,
                memory_used_mb=1200,
                memory_total_mb=2000,
                disk_used_gb=10.0,
                disk_total_gb=20.0,
                disk_percent=50.0,
            )
        ]
    )
    await db_session.commit()

    event_bus.reset()
    with (
        patch("app.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await _cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(HostResourceSample))
    remaining = result.scalars().all()
    assert len(remaining) == 2

    events = event_bus.get_recent_events(event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["host_resource_samples_deleted"] == 4


async def test_cleanup_capacity_snapshots_in_batches_and_reports_counts(db_session: AsyncSession) -> None:
    old_time = datetime.now(UTC) - timedelta(days=45)
    recent_time = datetime.now(UTC) - timedelta(days=1)
    db_session.add_all(
        [
            AnalyticsCapacitySnapshot(
                captured_at=old_time - timedelta(minutes=index),
                total_capacity_slots=3,
                active_sessions=1,
                queued_requests=0,
                available_capacity_slots=2,
            )
            for index in range(5)
        ]
        + [
            AnalyticsCapacitySnapshot(
                captured_at=recent_time,
                total_capacity_slots=4,
                active_sessions=2,
                queued_requests=1,
                available_capacity_slots=2,
            )
        ]
    )
    await db_session.commit()

    event_bus.reset()
    with (
        patch("app.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await _cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(AnalyticsCapacitySnapshot))
    remaining = result.scalars().all()
    assert len(remaining) == 2

    events = event_bus.get_recent_events(event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["capacity_snapshots_deleted"] == 4
