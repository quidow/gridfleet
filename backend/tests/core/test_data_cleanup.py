import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.analytics.models import AnalyticsCapacitySnapshot
from app.devices.models import DeviceEvent, DeviceEventType, DeviceRemediationLogEntry
from app.devices.services import data_cleanup
from app.devices.services.data_cleanup import DataCleanupService
from app.hosts.models import Host, HostResourceSample
from app.sessions.models import Session, SessionStatus
from app.settings.models import ConfigAuditLog
from tests.concurrency.group_lock_helpers import capture_statements
from tests.fakes import FakeSettingsReader
from tests.helpers import dispatch_committed_events, recent_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.services.data_cleanup import CleanupModel
    from app.events.catalog import EventSeverity


async def _create_device(db: AsyncSession, host: Host) -> uuid.UUID:
    """Create a minimal device for FK references."""
    from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType

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

    await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    result = await db_session.execute(select(Session))
    remaining = result.scalars().all()
    session_ids = {s.session_id for s in remaining}
    assert "old-session" not in session_ids
    assert "recent-session" in session_ids
    assert "running-session" in session_ids


async def test_cleanup_uses_separate_retention_window_for_probes(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sessions.probe_constants import PROBE_TEST_NAME
    from tests.conftest import settings_service

    overrides: dict[str, int] = {"retention.sessions_days": 30, "retention.probe_sessions_days": 7}
    original_get = settings_service.get

    def _get(key: str) -> object:
        if key in overrides:
            return overrides[key]
        return original_get(key)

    monkeypatch.setattr(settings_service, "get", _get)

    device_id = await _create_device(db_session, db_host)
    now = datetime.now(UTC)

    real_recent = Session(
        session_id="real-recent",
        device_id=device_id,
        test_name="test_login",
        started_at=now - timedelta(days=10),
        ended_at=now - timedelta(days=10),
        status=SessionStatus.passed,
    )
    real_old = Session(
        session_id="real-old",
        device_id=device_id,
        test_name="test_login",
        started_at=now - timedelta(days=40),
        ended_at=now - timedelta(days=40),
        status=SessionStatus.passed,
    )
    probe_recent = Session(
        session_id="probe-recent",
        device_id=device_id,
        test_name=PROBE_TEST_NAME,
        started_at=now - timedelta(days=3),
        ended_at=now - timedelta(days=3),
        status=SessionStatus.passed,
    )
    probe_old = Session(
        session_id="probe-old",
        device_id=device_id,
        test_name=PROBE_TEST_NAME,
        started_at=now - timedelta(days=10),
        ended_at=now - timedelta(days=10),
        status=SessionStatus.passed,
    )
    db_session.add_all([real_recent, real_old, probe_recent, probe_old])
    await db_session.commit()

    await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)
    await db_session.commit()

    remaining_ids = set((await db_session.execute(select(Session.session_id))).scalars().all())
    assert "real-recent" in remaining_ids
    assert "probe-recent" in remaining_ids
    assert "real-old" not in remaining_ids
    assert "probe-old" not in remaining_ids


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

    await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

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

    await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(DeviceEvent))
    remaining = result.scalars().all()
    assert len(remaining) == 1
    assert remaining[0].event_type == DeviceEventType.connectivity_restored


async def test_cleanup_prunes_old_remediation_log_entries(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    now = datetime.now(UTC)
    old_entry = DeviceRemediationLogEntry(
        device_id=device_id,
        kind="attempt",
        source="node_health",
        action="recovery_failed",
        at=now - timedelta(days=40),
    )
    recent_entry = DeviceRemediationLogEntry(
        device_id=device_id,
        kind="attempt",
        source="node_health",
        action="recovery_failed",
        at=now - timedelta(days=1),
    )
    db_session.add_all([old_entry, recent_entry])
    await db_session.commit()

    await DataCleanupService(
        publisher=event_bus,
        settings=FakeSettingsReader({"retention.remediation_log_days": 30}),
    ).cleanup_old_data(db_session)

    remaining = (await db_session.execute(select(DeviceRemediationLogEntry))).scalars().all()
    remaining_ids = {entry.id for entry in remaining}
    assert old_entry.id not in remaining_ids
    assert recent_entry.id in remaining_ids


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

    event_bus._log.clear()
    with (
        patch("app.devices.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.devices.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(Session))
    remaining = result.scalars().all()
    assert len(remaining) == 1
    await dispatch_committed_events()
    events = recent_events(event_bus, event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["sessions_deleted"] == 4
    assert events[0]["data"]["host_resource_samples_deleted"] == 0
    assert events[0]["data"]["duration_seconds"] >= 0.0


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

    event_bus._log.clear()
    with (
        patch("app.devices.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.devices.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(HostResourceSample))
    remaining = result.scalars().all()
    assert len(remaining) == 2

    await dispatch_committed_events()
    events = recent_events(event_bus, event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["host_resource_samples_deleted"] == 4


async def test_cleanup_purges_old_terminal_grid_tickets(db_session: AsyncSession, db_host: Host) -> None:
    """Terminal tickets (cancelled/expired) older than retention.sessions_days are
    deleted; waiting tickets are never touched."""
    from app.grid.models import GridQueueStatus, GridSessionQueueTicket

    await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)
    recent_time = datetime.now(UTC) - timedelta(days=1)

    def _ticket(status: GridQueueStatus, *, updated: datetime) -> GridSessionQueueTicket:
        return GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}},
            status=status,
            created_at=updated,
            updated_at=updated,
        )

    old_expired = _ticket(GridQueueStatus.expired, updated=old_time)
    old_cancelled = _ticket(GridQueueStatus.cancelled, updated=old_time)
    recent_expired = _ticket(GridQueueStatus.expired, updated=recent_time)
    old_waiting = _ticket(GridQueueStatus.waiting, updated=old_time)
    db_session.add_all([old_expired, old_cancelled, recent_expired, old_waiting])
    await db_session.commit()

    event_bus._log.clear()
    await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    remaining = {row.id for row in (await db_session.execute(select(GridSessionQueueTicket))).scalars().all()}
    assert old_expired.id not in remaining
    assert old_cancelled.id not in remaining
    assert recent_expired.id in remaining
    assert old_waiting.id in remaining

    await dispatch_committed_events()
    events = recent_events(event_bus, event_types=["system.cleanup_completed"])
    assert events[0]["data"]["grid_queue_tickets_deleted"] == 2


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
            )
            for index in range(5)
        ]
        + [
            AnalyticsCapacitySnapshot(
                captured_at=recent_time,
                total_capacity_slots=4,
                active_sessions=2,
                queued_requests=1,
            )
        ]
    )
    await db_session.commit()

    event_bus._log.clear()
    with (
        patch("app.devices.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.devices.services.data_cleanup.MAX_BATCHES_PER_TABLE", 2),
    ):
        await DataCleanupService(publisher=event_bus, settings=FakeSettingsReader({})).cleanup_old_data(db_session)

    from sqlalchemy import select

    result = await db_session.execute(select(AnalyticsCapacitySnapshot))
    remaining = result.scalars().all()
    assert len(remaining) == 2

    await dispatch_committed_events()
    events = recent_events(event_bus, event_types=["system.cleanup_completed"])
    assert len(events) == 1
    assert events[0]["data"]["capacity_snapshots_deleted"] == 4


async def test_delete_in_batches_batch_failure_preserves_earlier_batch_commits(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Each batch is its own transaction: a failure in batch two must not undo batch one.

    A different session (a fresh connection) proves batch one's delete is durable —
    checking through ``db_session`` itself would not distinguish "committed" from
    "still visible in the same open transaction".
    """
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)
    session_ids = [f"batch-fail-{index}" for index in range(4)]
    db_session.add_all(
        [
            Session(
                session_id=session_id,
                device_id=device_id,
                status=SessionStatus.passed,
                started_at=old_time - timedelta(minutes=index),
                ended_at=old_time - timedelta(minutes=index - 1),
            )
            for index, session_id in enumerate(session_ids)
        ]
    )
    await db_session.commit()

    real_delete_one_batch = data_cleanup._delete_one_batch
    call_count = 0

    async def _flaky_delete_one_batch(
        db: AsyncSession,
        *,
        model: CleanupModel,
        timestamp_column: InstrumentedAttribute[datetime],
        cutoff: datetime,
        extra_predicates: tuple[ColumnElement[bool], ...] = (),
    ) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("batch two boom")
        return await real_delete_one_batch(
            db,
            model=model,
            timestamp_column=timestamp_column,
            cutoff=cutoff,
            extra_predicates=extra_predicates,
        )

    with (
        patch("app.devices.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.devices.services.data_cleanup._delete_one_batch", _flaky_delete_one_batch),
        pytest.raises(RuntimeError, match="batch two boom"),
    ):
        await data_cleanup._delete_in_batches(
            db_session,
            model=Session,
            timestamp_column=Session.started_at,
            cutoff=old_time + timedelta(days=1),
        )

    async with db_session_maker() as verify_db:
        remaining = set(
            (await verify_db.execute(select(Session.session_id).where(Session.session_id.in_(session_ids)))).scalars()
        )
    # _delete_one_batch orders by timestamp_column.asc(): the two oldest rows
    # (highest index -> earliest started_at) are batch one and are gone for good;
    # the two most recent rows were queued for batch two, which never committed.
    assert remaining == {session_ids[0], session_ids[1]}, (
        "batch one (the two oldest session rows) must stay deleted even though batch two raised"
    )


async def test_cleanup_old_data_publishes_with_no_open_transaction(db_session: AsyncSession, db_host: Host) -> None:
    device_id = await _create_device(db_session, db_host)
    old_time = datetime.now(UTC) - timedelta(days=100)
    db_session.add_all(
        [
            Session(
                session_id=f"publish-check-{index}",
                device_id=device_id,
                status=SessionStatus.passed,
                started_at=old_time - timedelta(minutes=index),
                ended_at=old_time - timedelta(minutes=index - 1),
            )
            for index in range(5)
        ]
    )
    await db_session.commit()

    in_transaction_at_publish: list[bool] = []

    class _RecordingPublisher:
        async def publish(
            self,
            event_type: str,
            data: dict[str, object],
            *,
            severity: EventSeverity | None = None,
        ) -> None:
            del event_type, data, severity
            in_transaction_at_publish.append(db_session.in_transaction())

    with (
        patch("app.devices.services.data_cleanup.DELETE_BATCH_SIZE", 2),
        patch("app.devices.services.data_cleanup.MAX_BATCHES_PER_TABLE", 3),
    ):
        await DataCleanupService(publisher=_RecordingPublisher(), settings=FakeSettingsReader({})).cleanup_old_data(
            db_session
        )

    assert in_transaction_at_publish == [False]


async def test_delete_in_batches_zero_rows_commits_once_and_leaves_no_open_transaction(
    db_session: AsyncSession,
) -> None:
    """An empty table's terminal batch still commits, so it never leaves an idle read
    transaction on the shared session — and the loop stops after that single attempt
    rather than probing again."""
    assert not db_session.in_transaction()

    async with capture_statements(db_session) as statements:
        deleted_total = await data_cleanup._delete_in_batches(
            db_session,
            model=Session,
            timestamp_column=Session.started_at,
            cutoff=datetime.now(UTC),
        )

    delete_statements = [sql for sql in statements if sql.lstrip().upper().startswith("DELETE")]
    assert deleted_total == 0
    assert len(delete_statements) == 1
    assert not db_session.in_transaction()
