import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.schemas.run import DeviceRequirement, ReservedDeviceInfo
from app.services import run_service
from app.services.cursor_pagination import encode_cursor
from tests.helpers import create_device, create_reserved_run


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

    monkeypatch.setattr(
        "app.services.run_service.config_service.get_device_config", AsyncMock(side_effect=RuntimeError)
    )
    monkeypatch.setattr(
        "app.services.run_service.capability_service.get_device_capabilities",
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
    monkeypatch.setattr("app.services.run_service._readiness_for_match", AsyncMock(return_value=True))

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

    monkeypatch.setattr(
        "app.services.run_service.revoke_intents_and_reconcile",
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
    monkeypatch.setattr("app.services.run_service.settings_service.get", lambda key: settings[key])
    monkeypatch.setattr("app.services.run_service.register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr("app.services.run_service.lifecycle_incident_service.record_lifecycle_incident", AsyncMock())

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

    monkeypatch.setattr("app.services.run_service.grid_service.terminate_grid_session", AsyncMock(return_value=False))
    monkeypatch.setattr("app.services.device_state.queue_event_for_session", lambda *args, **kwargs: None)
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

    monkeypatch.setattr("app.services.run_service.revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr("app.services.run_service.lifecycle_policy_actions.record_ci_preparation_failed", AsyncMock())
    monkeypatch.setattr("app.services.run_service.maintenance_service.enter_maintenance", AsyncMock())
    monkeypatch.setattr("app.services.run_service.device_health.update_device_checks", AsyncMock())
    monkeypatch.setattr("app.services.run_service.lifecycle_incident_service.record_lifecycle_incident", AsyncMock())

    with pytest.raises(ValueError, match="message is required"):
        await run_service.report_preparation_failure(db_session, run.id, device.id, message="  ")
    with pytest.raises(ValueError, match="not actively reserved"):
        await run_service.report_preparation_failure(db_session, run.id, other_device.id, message="bad")

    refreshed = await run_service.report_preparation_failure(db_session, run.id, device.id, message="bad setup")
    assert refreshed.id == run.id
    assert refreshed.device_reservations[0].excluded is True
    assert refreshed.device_reservations[0].exclusion_reason == "bad setup"

    monkeypatch.setattr(
        "app.services.run_service.settings_service.get",
        lambda key: {
            "general.device_cooldown_max_sec": 60,
            "general.device_cooldown_escalation_threshold": 1,
        }[key],
    )
    monkeypatch.setattr("app.services.run_service.register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr("app.services.run_service.lifecycle_policy_actions.exclude_run_if_needed", AsyncMock())
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
    monkeypatch.setattr("app.services.run_service.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))

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

    monkeypatch.setattr("app.services.run_service.grid_service.terminate_grid_session", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.device_state.queue_event_for_session", lambda *args, **kwargs: None)
    pending = await run_service._release_devices(db_session, run, commit=False, terminate_grid_sessions=True)

    assert set(pending) == {maintenance.id, busy.id, odd.id}
