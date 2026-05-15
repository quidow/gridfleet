import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import encode_cursor
from app.devices.models import DeviceHold, DeviceOperationalState
from app.hosts.models import Host
from app.runs import service as run_service
from app.runs import service_lifecycle_release as run_lifecycle_release
from app.runs.models import RunState, TestRun
from app.runs.schemas import DeviceRequirement, ReservedDeviceInfo
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device, create_reserved_run

RUN_FAILURES_MODULE = "app.runs.service_lifecycle_failures"
RUN_RELEASE_MODULE = "app.runs.service_lifecycle_release"
RUN_LOOKUP_MODULE = "app.runs.service_reservation_lookup"


async def test_run_service_include_and_hydration_error_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Hydration Device",
        identity_value="run-hydrate-001",
        operational_state=DeviceOperationalState.available,
        test_data={"token": "secret"},
    )
    info = ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        name=device.name,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )

    monkeypatch.setattr("app.runs.service_query.config_service.get_device_config", AsyncMock(side_effect=RuntimeError))
    monkeypatch.setattr(
        "app.runs.service_query.capability_service.get_device_capabilities",
        AsyncMock(side_effect=ValueError),
    )

    await run_service.hydrate_reserved_device_info(
        db_session,
        info,
        device,
        includes={"config", "capabilities", "test_data"},
    )

    assert info.config is None
    assert info.live_capabilities is None
    assert info.test_data == {"token": "secret"}
    assert [(item.include, item.reason) for item in info.unavailable_includes or []] == [
        ("config", "RuntimeError"),
        ("capabilities", "ValueError"),
    ]

    run_service.mark_reserved_device_info_includes_unavailable(
        info,
        includes={"config", "capabilities", "test_data"},
        reason="not loaded",
    )
    assert info.test_data is None
    assert {item.include for item in info.unavailable_includes or []} == {"config", "capabilities", "test_data"}

    class BrokenTestDataDevice:
        @property
        def test_data(self) -> dict[str, object]:
            raise RuntimeError("json decode failed")

    broken_info = ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        name=device.name,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    await run_service.hydrate_reserved_device_info(
        db_session,
        broken_info,
        BrokenTestDataDevice(),  # type: ignore[arg-type]
        includes={"test_data"},
    )
    assert broken_info.test_data is None
    assert broken_info.unavailable_includes is not None
    assert broken_info.unavailable_includes[0].reason == "RuntimeError"


async def test_find_matching_devices_filters_os_tags_and_allocation(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wanted = await create_device(
        db_session,
        host_id=db_host.id,
        name="Wanted Device",
        identity_value="run-match-001",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        tags={"pool": "smoke"},
    )
    await create_device(
        db_session,
        host_id=db_host.id,
        name="Wrong OS Device",
        identity_value="run-match-002",
        os_version="13",
        operational_state=DeviceOperationalState.available,
        tags={"pool": "smoke"},
    )
    await create_device(
        db_session,
        host_id=db_host.id,
        name="Wrong Tag Device",
        identity_value="run-match-003",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        tags={"pool": "regression"},
    )
    monkeypatch.setattr("app.runs.service_allocator._readiness_for_match", AsyncMock(return_value=True))

    req = DeviceRequirement(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        os_version="14",
        allocation="all_available",
        min_count=1,
        tags={"pool": "smoke"},
    )

    matches = await run_service._find_matching_devices(db_session, req)

    assert [device.id for device in matches] == [wanted.id]
    assert run_service._minimum_required_count(req) == 1
    assert run_service._select_matching_devices(req, matches) == matches
    assert run_service._format_requirement_count(req) == "allocation=all_available, min_count=1"


async def test_run_listing_cursor_and_state_transition_branches(db_session: AsyncSession) -> None:
    older = TestRun(name="older", state=RunState.preparing, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30)
    active = TestRun(name="active", state=RunState.active, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30)
    terminal = TestRun(
        name="terminal", state=RunState.completed, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30
    )
    db_session.add_all([older, active, terminal])
    await db_session.flush()
    older.created_at = datetime.now(UTC) - timedelta(minutes=3)
    active.created_at = datetime.now(UTC) - timedelta(minutes=2)
    terminal.created_at = datetime.now(UTC) - timedelta(minutes=1)
    terminal.completed_at = datetime.now(UTC)
    await db_session.commit()

    listed, total = await run_service.list_runs(db_session, sort_by="duration", sort_dir="asc")
    assert total == 3
    assert {run.name for run in listed} == {"older", "active", "terminal"}

    filtered_page = await run_service.list_runs_cursor(
        db_session,
        state=RunState.active,
        created_from=active.created_at - timedelta(seconds=1),
        created_to=active.created_at + timedelta(seconds=1),
    )
    assert [run.id for run in filtered_page.items] == [active.id]

    newer_page = await run_service.list_runs_cursor(
        db_session,
        cursor=encode_cursor(older.created_at, older.id),
        direction="newer",
        limit=2,
    )
    assert [run.id for run in newer_page.items] == [terminal.id, active.id]

    empty_page = await run_service.list_runs_cursor(
        db_session,
        cursor=encode_cursor(datetime.now(UTC) - timedelta(days=1), uuid.uuid4()),
    )
    assert empty_page.items == []

    with pytest.raises(ValueError, match="Run not found"):
        await run_service.signal_ready(db_session, uuid.uuid4())
    with pytest.raises(ValueError, match="Cannot signal ready"):
        await run_service.signal_ready(db_session, active.id)

    ready = await run_service.signal_ready(db_session, older.id)
    assert ready.state == RunState.active
    assert ready.started_at is not None

    already_active = await run_service.signal_active(db_session, active.id)
    assert already_active.state == RunState.active
    with pytest.raises(ValueError, match="Cannot signal active"):
        await run_service.signal_active(db_session, terminal.id)

    before = terminal.last_heartbeat
    heartbeat_terminal = await run_service.heartbeat(db_session, terminal.id)
    assert heartbeat_terminal.last_heartbeat == before
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.heartbeat(db_session, uuid.uuid4())


async def test_run_terminal_transition_paths(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.runs.service_lifecycle._clear_desired_grid_run_id_for_run", AsyncMock())
    monkeypatch.setattr("app.runs.service_lifecycle._release_devices", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.runs.service_lifecycle._complete_deferred_stops_post_commit", AsyncMock())
    monkeypatch.setattr("app.runs.service_lifecycle.queue_event_for_session", lambda *args, **kwargs: None)

    active = TestRun(
        name="complete-me",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=30,
        started_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    cancel = TestRun(name="cancel-me", state=RunState.active, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30)
    force = TestRun(name="force-me", state=RunState.active, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30)
    expired = TestRun(
        name="expire-me", state=RunState.active, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30
    )
    terminal = TestRun(
        name="already-done", state=RunState.completed, requirements=[], ttl_minutes=10, heartbeat_timeout_sec=30
    )
    db_session.add_all([active, cancel, force, expired, terminal])
    await db_session.commit()

    with pytest.raises(ValueError, match="Run not found"):
        await run_service.complete_run(db_session, uuid.uuid4())
    with pytest.raises(ValueError, match="terminal state"):
        await run_service.complete_run(db_session, terminal.id)
    completed = await run_service.complete_run(db_session, active.id)
    assert completed.state == RunState.completed
    assert completed.completed_at is not None

    with pytest.raises(ValueError, match="Run not found"):
        await run_service.cancel_run(db_session, uuid.uuid4())
    cancelled = await run_service.cancel_run(db_session, cancel.id)
    assert cancelled.state == RunState.cancelled

    with pytest.raises(ValueError, match="Run not found"):
        await run_service.force_release(db_session, uuid.uuid4())
    forced = await run_service.force_release(db_session, force.id)
    assert forced.state == RunState.cancelled
    assert forced.error == "Force released by admin"

    await run_service.expire_run(db_session, expired, "timeout")
    await db_session.refresh(expired)
    assert expired.state == RunState.expired
    assert expired.error == "timeout"

    before = terminal.completed_at
    await run_service.expire_run(db_session, terminal, "ignored")
    await db_session.refresh(terminal)
    assert terminal.completed_at == before


async def test_restore_and_exclude_device_reservation_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Reservation Branch Device",
        identity_value="run-reservation-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="reservation-branch-run", devices=[device])
    entry = run.device_reservations[0]

    assert await run_service.exclude_device_from_run(db_session, uuid.uuid4(), reason="missing") is None
    assert await run_service.get_device_reservation(db_session, device.id) == run
    reservation_map = await run_service.get_device_reservation_map(db_session, [device.id])
    assert reservation_map[device.id] == run
    assert run_service.get_reservation_context_for_device(None, device.id) == (None, None)
    assert run_service.get_reservation_context_for_device(run, uuid.uuid4()) == (run, None)

    monkeypatch.setattr(
        f"{RUN_LOOKUP_MODULE}.revoke_intents_and_reconcile",
        AsyncMock(),
    )
    excluded = await run_service.exclude_device_from_run(db_session, device.id, reason="bad device", commit=False)
    assert excluded is not None
    assert entry.excluded is True
    same_exclusion = await run_service.exclude_device_from_run(db_session, device.id, reason="bad device", commit=False)
    assert same_exclusion is excluded

    entry.excluded_until = datetime.now(UTC) + timedelta(minutes=5)
    still_excluded = await run_service.restore_device_to_run(db_session, device.id, commit=False)
    assert still_excluded is excluded

    entry.excluded_until = None
    restored = await run_service.restore_device_to_run(db_session, device.id, commit=False)
    assert restored is excluded
    assert entry.excluded is False
    assert entry.exclusion_reason is None
    assert await run_service.restore_device_to_run(db_session, device.id, commit=False) is excluded

    monkeypatch.setattr(f"{RUN_LOOKUP_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    committed_excluded = await run_service.exclude_device_from_run(db_session, device.id, reason="missing lock")
    assert committed_excluded is not None
    committed_restored = await run_service.restore_device_to_run(db_session, device.id)
    assert committed_restored is not None

    assert await run_service.restore_device_to_run(db_session, uuid.uuid4()) is None


async def test_cooldown_device_guard_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Cooldown Device",
        identity_value="run-cooldown-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="cooldown-run", devices=[device], state=RunState.active)
    settings = {
        "general.device_cooldown_max_sec": 30,
        "general.device_cooldown_escalation_threshold": 3,
    }
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.settings_service.get", lambda key: settings[key])
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.lifecycle_incident_service.record_lifecycle_incident", AsyncMock())

    with pytest.raises(ValueError, match="ttl_seconds"):
        await run_service.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=31)
    with pytest.raises(ValueError, match="Cooldown reason"):
        await run_service.cooldown_device(db_session, run.id, device.id, reason=" ", ttl_seconds=5)
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.cooldown_device(db_session, uuid.uuid4(), device.id, reason="flaky", ttl_seconds=5)

    run.state = RunState.completed
    await db_session.commit()
    with pytest.raises(ValueError, match="terminal run"):
        await run_service.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=5)

    run.state = RunState.active
    await db_session.commit()
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    with pytest.raises(ValueError, match="Device not found"):
        await run_service.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=5)

    other_device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Cooldown Other Device",
        identity_value="run-cooldown-002",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(return_value=other_device))
    with pytest.raises(ValueError, match="not actively reserved"):
        await run_service.cooldown_device(db_session, run.id, other_device.id, reason="flaky", ttl_seconds=5)

    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(return_value=device))
    excluded_until, count, escalated, threshold = await run_service.cooldown_device(
        db_session,
        run.id,
        device.id,
        reason="flaky",
        ttl_seconds=5,
    )

    assert excluded_until is not None
    assert (count, escalated, threshold) == (1, False, 3)


async def test_release_devices_branches_and_session_counts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Device",
        identity_value="run-release-001",
        operational_state=DeviceOperationalState.busy,
        hold=DeviceHold.reserved,
    )
    run = await create_reserved_run(db_session, name="release-run", devices=[device], state=RunState.active)
    db_session.add_all(
        [
            Session(session_id="release-running", device_id=device.id, run_id=run.id, status=SessionStatus.running),
            Session(session_id="release-passed", device_id=device.id, run_id=run.id, status=SessionStatus.passed),
            Session(session_id="release-orphan", status=SessionStatus.failed),
        ]
    )
    await db_session.commit()
    await db_session.refresh(run, attribute_names=["device_reservations"])

    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.grid_service.terminate_grid_session", AsyncMock(return_value=False))
    monkeypatch.setattr("app.devices.services.state.queue_event_for_session", lambda *args, **kwargs: None)
    pending_ids = await run_service._release_devices(db_session, run, commit=False, terminate_grid_sessions=True)
    assert pending_ids == [device.id]
    assert run.device_reservations[0].released_at is not None

    counts = await run_service.fetch_session_counts(db_session, [run.id])
    assert counts[run.id].running == 1
    assert counts[run.id].passed == 1
    assert await run_service.fetch_session_counts(db_session, []) == {}

    read = run_service.build_run_read(run, counts[run.id])
    assert read.session_counts.total == 2

    empty = TestRun(name="empty", state=RunState.active, requirements=[], ttl_minutes=1, heartbeat_timeout_sec=1)
    empty.device_reservations = []
    assert await run_service._release_devices(db_session, empty, commit=True) == []


async def test_mark_running_sessions_released_success_path(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Session Device",
        identity_value="run-release-session-001",
        operational_state=DeviceOperationalState.busy,
        hold=DeviceHold.reserved,
    )
    run = await create_reserved_run(db_session, name="release-session-run", devices=[device], state=RunState.cancelled)
    session = Session(session_id="release-success", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.grid_service.terminate_grid_session", AsyncMock(return_value=True))
    await run_service._mark_running_sessions_released(
        db_session,
        run,
        datetime.now(UTC),
        terminate_grid_sessions=True,
    )

    assert session.status == SessionStatus.error
    assert session.ended_at is not None
    assert session.error_type == "run_released"

    untouched = Session(
        session_id="release-untouched", device_id=device.id, run_id=run.id, status=SessionStatus.running
    )
    db_session.add(untouched)
    await db_session.flush()
    await run_service._mark_running_sessions_released(
        db_session,
        run,
        datetime.now(UTC),
        terminate_grid_sessions=False,
    )
    assert untouched.status == SessionStatus.running


async def test_report_preparation_failure_and_cooldown_escalation_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Preparation Failure Device",
        identity_value="run-prep-failure-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="prep-failure-run", devices=[device], state=RunState.active)
    other_device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Other Device",
        identity_value="run-prep-failure-002",
        operational_state=DeviceOperationalState.available,
    )

    monkeypatch.setattr(f"{RUN_LOOKUP_MODULE}.revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.lifecycle_policy_actions.record_ci_preparation_failed", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}._enter_maintenance", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_health.update_device_checks", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.lifecycle_incident_service.record_lifecycle_incident", AsyncMock())

    with pytest.raises(ValueError, match="message is required"):
        await run_service.report_preparation_failure(db_session, run.id, device.id, message="  ")
    with pytest.raises(ValueError, match="not actively reserved"):
        await run_service.report_preparation_failure(db_session, run.id, other_device.id, message="bad")

    refreshed = await run_service.report_preparation_failure(db_session, run.id, device.id, message="bad setup")
    assert refreshed.id == run.id
    assert refreshed.device_reservations[0].excluded is True
    assert refreshed.device_reservations[0].exclusion_reason == "bad setup"

    monkeypatch.setattr(
        f"{RUN_FAILURES_MODULE}.settings_service.get",
        lambda key: {
            "general.device_cooldown_max_sec": 60,
            "general.device_cooldown_escalation_threshold": 1,
        }[key],
    )
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.lifecycle_policy_actions.exclude_run_if_needed", AsyncMock())
    escalated_until, count, escalated, threshold = await run_service.cooldown_device(
        db_session,
        refreshed.id,
        device.id,
        reason="still flaky",
        ttl_seconds=5,
    )
    assert escalated_until is None
    assert (count, escalated, threshold) == (1, True, 1)


async def test_report_preparation_failure_missing_device_path(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Missing During Prep Device",
        identity_value="run-prep-missing-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="prep-missing-run", devices=[device], state=RunState.active)
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))

    with pytest.raises(ValueError, match="Device not found"):
        await run_service.report_preparation_failure(db_session, run.id, device.id, message="bad setup")


async def test_release_devices_unusual_restore_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maintenance = await create_device(
        db_session,
        host_id=db_host.id,
        name="Maintenance Release Device",
        identity_value="run-release-maint-001",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.maintenance,
    )
    busy = await create_device(
        db_session,
        host_id=db_host.id,
        name="Busy Release Device",
        identity_value="run-release-busy-001",
        operational_state=DeviceOperationalState.busy,
        hold=DeviceHold.reserved,
    )
    odd = await create_device(
        db_session,
        host_id=db_host.id,
        name="Odd Release Device",
        identity_value="run-release-odd-001",
        operational_state=DeviceOperationalState.offline,
        hold=None,
    )
    run = await create_reserved_run(
        db_session,
        name="release-unusual-run",
        devices=[maintenance, busy, odd],
        state=RunState.cancelled,
    )
    db_session.add(Session(session_id="busy-running", device_id=busy.id, run_id=run.id, status=SessionStatus.running))
    await db_session.commit()
    await db_session.refresh(run, attribute_names=["device_reservations"])

    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.grid_service.terminate_grid_session", AsyncMock(return_value=True))
    monkeypatch.setattr("app.devices.services.state.queue_event_for_session", lambda *args, **kwargs: None)
    pending = await run_service._release_devices(db_session, run, commit=False, terminate_grid_sessions=True)

    assert set(pending) == {maintenance.id, busy.id, odd.id}


async def test_release_devices_handles_missing_maintenance_and_already_restored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_id = uuid.uuid4()
    maintenance_id = uuid.uuid4()
    restored_id = uuid.uuid4()
    reservations = [
        SimpleNamespace(id=uuid.uuid4(), device_id=missing_id, released_at=None),
        SimpleNamespace(id=uuid.uuid4(), device_id=maintenance_id, released_at=None),
        SimpleNamespace(id=uuid.uuid4(), device_id=restored_id, released_at=None),
    ]
    run = SimpleNamespace(
        id=uuid.uuid4(),
        name="fake-release",
        state=RunState.cancelled,
        device_reservations=reservations,
    )
    db = AsyncMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}._mark_running_sessions_released", AsyncMock())
    monkeypatch.setattr(
        f"{RUN_RELEASE_MODULE}.device_locking.lock_devices",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=maintenance_id,
                    hold=DeviceHold.maintenance,
                    operational_state=DeviceOperationalState.available,
                ),
                SimpleNamespace(id=restored_id, hold=None, operational_state=DeviceOperationalState.available),
            ]
        ),
    )

    pending = await run_service._release_devices(db, run, commit=True)

    assert pending == [maintenance_id, restored_id]
    assert all(reservation.released_at is not None for reservation in reservations)
    db.commit.assert_awaited_once()


async def test_report_preparation_failure_missing_and_terminal_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.get_run", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.report_preparation_failure(AsyncMock(), uuid.uuid4(), uuid.uuid4(), message="bad")

    terminal = TestRun(
        id=uuid.uuid4(),
        name="terminal-prep",
        state=RunState.completed,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=30,
    )
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.get_run", AsyncMock(return_value=terminal))
    with pytest.raises(ValueError, match="terminal run"):
        await run_service.report_preparation_failure(AsyncMock(), terminal.id, uuid.uuid4(), message="bad")


async def test_run_service_small_async_branch_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_service._reserved_entry_is_excluded(
        SimpleNamespace(excluded=True, excluded_until=datetime.now(UTC) + timedelta(minutes=1))
    )
    monkeypatch.setattr("app.runs.service_allocator.settings_service.get", lambda key: 10)
    with pytest.raises(ValueError, match="exceeds maximum"):
        run_service._resolve_run_options(
            SimpleNamespace(ttl_minutes=20, heartbeat_timeout_sec=None),
        )

    class CountsResult:
        def all(self) -> list[tuple[uuid.UUID | None, object, int]]:
            run_id = uuid.uuid4()
            return [(None, SessionStatus.failed, 2), (run_id, "custom", 3)]

    class CountsSession:
        async def execute(self, *_args: object, **_kwargs: object) -> CountsResult:
            return CountsResult()

    counts = await run_service.fetch_session_counts(CountsSession(), [uuid.uuid4()])  # type: ignore[arg-type]
    assert len(counts) == 1
    assert next(iter(counts.values())).total == 3

    missing_device_id = uuid.uuid4()

    class DeferredSession:
        async def get(self, *_args: object, **_kwargs: object) -> object | None:
            return None

    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.lifecycle_policy.complete_deferred_stop_if_session_ended", AsyncMock())
    await run_service._complete_deferred_stops_post_commit(DeferredSession(), [missing_device_id])  # type: ignore[arg-type]
    run_lifecycle_release.lifecycle_policy.complete_deferred_stop_if_session_ended.assert_not_awaited()


async def test_clear_desired_grid_run_id_skips_released_and_missing_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    released = SimpleNamespace(device_id=uuid.uuid4(), released_at=datetime.now(UTC))
    active = SimpleNamespace(device_id=uuid.uuid4(), released_at=None)
    run = SimpleNamespace(id=uuid.uuid4(), device_reservations=[released, active])
    db = AsyncMock()
    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    revoke = AsyncMock()
    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.revoke_intents_and_reconcile", revoke)

    await run_service._clear_desired_grid_run_id_for_run(db, run=run, caller="run_completed")

    revoke.assert_not_awaited()
