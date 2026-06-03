import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.core.pagination import encode_cursor
from app.devices.models import DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.intent import IntentService
from app.devices.services.maintenance import MaintenanceService
from app.grid.service import GridService
from app.hosts.models import Host
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs import service as run_service
from app.runs.models import RunState, TestRun
from app.runs.schemas import DeviceRequirement, ReservedDeviceInfo
from app.runs.service_allocator import (
    RunAllocatorService,
    _find_matching_devices,
    _format_requirement_count,
    _minimum_required_count,
    _select_matching_devices,
)
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, build_review_service, make_fake_grid
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

RUN_FAILURES_MODULE = "app.runs.service_lifecycle_failures"
RUN_RELEASE_MODULE = "app.runs.service_lifecycle_release"
RUN_LOOKUP_MODULE = "app.runs.service_reservation"

_settings = FakeSettingsReader({})
_grid = GridService(settings=_settings)
_circuit_breaker = AgentCircuitBreaker(publisher=event_bus, settings=_settings)
_query_svc = RunQueryService(capability=DeviceCapabilityService())
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    grid=_grid,
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=_release_svc)
_failure_svc = RunFailureService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=_circuit_breaker,
    maintenance=MaintenanceService(review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus),
    lifecycle_actions=AsyncMock(),
    reservation=RunReservationService(review=build_review_service()),
    health=AsyncMock(),
    incidents=LifecycleIncidentService(),
)


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
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(side_effect=ValueError),
    )

    await _query_svc.hydrate_reserved_device_info(
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
    await _query_svc.hydrate_reserved_device_info(
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

    matches = await _find_matching_devices(db_session, req)

    assert [device.id for device in matches] == [wanted.id]
    assert _minimum_required_count(req) == 1
    assert _select_matching_devices(req, matches) == matches
    assert _format_requirement_count(req) == "allocation=all_available, min_count=1"


async def test_find_matching_devices_matches_firetv_routing_major(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allocator matches Fire TV via routing major even when display version is the long marketing string."""
    wanted = await create_device(
        db_session,
        host_id=db_host.id,
        name="Fire TV Stick 4K",
        identity_value="firetv-route-guard",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        os_version="6",
        os_version_display="6.7.1.1",
        operational_state=DeviceOperationalState.available,
    )
    monkeypatch.setattr("app.runs.service_allocator._readiness_for_match", AsyncMock(return_value=True))

    req = DeviceRequirement(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        os_version="6",
        allocation="all_available",
        min_count=1,
    )

    matches = await _find_matching_devices(db_session, req)

    assert wanted.id in {device.id for device in matches}


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

    listed, total = await _query_svc.list_runs(db_session, sort_by="duration", sort_dir="asc")
    assert total == 3
    assert {run.name for run in listed} == {"older", "active", "terminal"}

    filtered_page = await _query_svc.list_runs_cursor(
        db_session,
        state=RunState.active,
        created_from=active.created_at - timedelta(seconds=1),
        created_to=active.created_at + timedelta(seconds=1),
    )
    assert [run.id for run in filtered_page.items] == [active.id]

    newer_page = await _query_svc.list_runs_cursor(
        db_session,
        cursor=encode_cursor(older.created_at, older.id),
        direction="newer",
        limit=2,
    )
    assert [run.id for run in newer_page.items] == [terminal.id, active.id]

    empty_page = await _query_svc.list_runs_cursor(
        db_session,
        cursor=encode_cursor(datetime.now(UTC) - timedelta(days=1), uuid.uuid4()),
    )
    assert empty_page.items == []

    with pytest.raises(ValueError, match="Run not found"):
        await _lifecycle_svc.signal_ready(db_session, uuid.uuid4())
    with pytest.raises(ValueError, match="Cannot signal ready"):
        await _lifecycle_svc.signal_ready(db_session, active.id)

    ready = await _lifecycle_svc.signal_ready(db_session, older.id)
    assert ready.state == RunState.active
    assert ready.started_at is not None

    already_active = await _lifecycle_svc.signal_active(db_session, active.id)
    assert already_active.state == RunState.active
    with pytest.raises(ValueError, match="Cannot signal active"):
        await _lifecycle_svc.signal_active(db_session, terminal.id)

    before = terminal.last_heartbeat
    heartbeat_terminal = await _lifecycle_svc.heartbeat(db_session, terminal.id)
    assert heartbeat_terminal.last_heartbeat == before
    with pytest.raises(ValueError, match="Run not found"):
        await _lifecycle_svc.heartbeat(db_session, uuid.uuid4())


async def test_run_terminal_transition_paths(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)

    mock_release = AsyncMock()
    mock_release.release_devices = AsyncMock(return_value=[])
    mock_release.clear_desired_grid_run_id_for_run = AsyncMock()
    mock_release.complete_deferred_stops_post_commit = AsyncMock()
    lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=mock_release)

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
        await lifecycle.complete_run(db_session, uuid.uuid4())
    with pytest.raises(ValueError, match="terminal state"):
        await lifecycle.complete_run(db_session, terminal.id)
    completed = await lifecycle.complete_run(db_session, active.id)
    assert completed.state == RunState.completed
    assert completed.completed_at is not None

    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.cancel_run(db_session, uuid.uuid4())
    cancelled = await lifecycle.cancel_run(db_session, cancel.id)
    assert cancelled.state == RunState.cancelled

    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.force_release(db_session, uuid.uuid4())
    forced = await lifecycle.force_release(db_session, force.id)
    assert forced.state == RunState.cancelled
    assert forced.error == "Force released by admin"

    await lifecycle.expire_run(db_session, expired, "timeout")
    await db_session.refresh(expired)
    assert expired.state == RunState.expired
    assert expired.error == "timeout"

    before = terminal.completed_at
    await lifecycle.expire_run(db_session, terminal, "ignored")
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
    svc = RunReservationService(review=build_review_service())

    assert await svc.exclude_device_from_run(db_session, uuid.uuid4(), reason="missing", publisher=event_bus) is None
    assert await run_service.get_device_reservation(db_session, device.id) == run
    reservation_map = await run_service.get_device_reservation_map(db_session, [device.id])
    assert reservation_map[device.id] == run
    assert run_service.get_reservation_context_for_device(None, device.id) == (None, None)
    assert run_service.get_reservation_context_for_device(run, uuid.uuid4()) == (run, None)

    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    excluded = await svc.exclude_device_from_run(
        db_session, device.id, reason="bad device", commit=False, publisher=event_bus
    )
    assert excluded is not None
    assert entry.excluded is True
    same_exclusion = await svc.exclude_device_from_run(
        db_session, device.id, reason="bad device", commit=False, publisher=event_bus
    )
    assert same_exclusion is excluded

    entry.excluded_until = datetime.now(UTC) + timedelta(minutes=5)
    still_excluded = await svc.restore_device_to_run(db_session, device.id, commit=False)
    assert still_excluded is excluded

    entry.excluded_until = None
    restored = await svc.restore_device_to_run(db_session, device.id, commit=False)
    assert restored is excluded
    assert entry.excluded is False
    assert entry.exclusion_reason is None
    assert await svc.restore_device_to_run(db_session, device.id, commit=False) is excluded

    monkeypatch.setattr(f"{RUN_LOOKUP_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    committed_excluded = await svc.exclude_device_from_run(
        db_session, device.id, reason="missing lock", publisher=event_bus
    )
    assert committed_excluded is not None
    committed_restored = await svc.restore_device_to_run(db_session, device.id)
    assert committed_restored is not None

    assert await svc.restore_device_to_run(db_session, uuid.uuid4()) is None


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
    fake_settings = FakeSettingsReader(
        {
            "general.device_cooldown_max_sec": 30,
            "general.device_cooldown_escalation_threshold": 3,
        }
    )
    failure_svc = RunFailureService(
        publisher=event_bus,
        settings=fake_settings,
        circuit_breaker=_circuit_breaker,
        maintenance=MaintenanceService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        health=AsyncMock(),
        incidents=LifecycleIncidentService(),
    )
    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", AsyncMock())
    failure_svc._incidents = AsyncMock()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="ttl_seconds"):
        await failure_svc.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=31)
    with pytest.raises(ValueError, match="Cooldown reason"):
        await failure_svc.cooldown_device(db_session, run.id, device.id, reason=" ", ttl_seconds=5)
    with pytest.raises(ValueError, match="Run not found"):
        await failure_svc.cooldown_device(db_session, uuid.uuid4(), device.id, reason="flaky", ttl_seconds=5)

    run.state = RunState.completed
    await db_session.commit()
    with pytest.raises(ValueError, match="terminal run"):
        await failure_svc.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=5)

    run.state = RunState.active
    await db_session.commit()
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    with pytest.raises(ValueError, match="Device not found"):
        await failure_svc.cooldown_device(db_session, run.id, device.id, reason="flaky", ttl_seconds=5)

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
        await failure_svc.cooldown_device(db_session, run.id, other_device.id, reason="flaky", ttl_seconds=5)

    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.device_locking.lock_device", AsyncMock(return_value=device))
    excluded_until, count, escalated, threshold = await failure_svc.cooldown_device(
        db_session, run.id, device.id, reason="flaky", ttl_seconds=5
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

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    pending_ids = await _release_svc.release_devices(
        db_session,
        run,
        commit=False,
        terminate_grid_sessions=True,
    )
    assert pending_ids == [device.id]
    assert run.device_reservations[0].released_at is not None

    counts = await _query_svc.fetch_session_counts(db_session, [run.id])
    assert counts[run.id].running == 1
    assert counts[run.id].passed == 1
    assert await _query_svc.fetch_session_counts(db_session, []) == {}

    read = run_service.build_run_read(run, counts[run.id])
    assert read.session_counts.total == 2

    empty = TestRun(name="empty", state=RunState.active, requirements=[], ttl_minutes=1, heartbeat_timeout_sec=1)
    empty.device_reservations = []
    assert await _release_svc.release_devices(db_session, empty, commit=True) == []


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
    )
    run = await create_reserved_run(db_session, name="release-session-run", devices=[device], state=RunState.cancelled)
    session = Session(session_id="release-success", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Use a fake grid that successfully terminates sessions.
    release_with_fake_grid = RunReleaseService(
        publisher=event_bus,
        settings=_settings,
        grid=make_fake_grid(),
        deferred_stop=AsyncMock(),
    )
    await release_with_fake_grid._mark_running_sessions_released(
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
    await release_with_fake_grid._mark_running_sessions_released(
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

    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(RunFailureService, "_enter_maintenance", AsyncMock())
    # health.update_device_checks is already AsyncMock via _failure_svc health stub
    _failure_svc._incidents = AsyncMock()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="message is required"):
        await _failure_svc.report_preparation_failure(db_session, run.id, device.id, message="  ")
    with pytest.raises(ValueError, match="not actively reserved"):
        await _failure_svc.report_preparation_failure(db_session, run.id, other_device.id, message="bad")

    refreshed = await _failure_svc.report_preparation_failure(db_session, run.id, device.id, message="bad setup")
    assert refreshed.id == run.id
    assert refreshed.device_reservations[0].excluded is True
    assert refreshed.device_reservations[0].exclusion_reason == "bad setup"

    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", AsyncMock())
    escalate_failure_svc = RunFailureService(
        publisher=event_bus,
        settings=FakeSettingsReader(
            {"general.device_cooldown_max_sec": 60, "general.device_cooldown_escalation_threshold": 1}
        ),
        circuit_breaker=_circuit_breaker,
        maintenance=MaintenanceService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        health=AsyncMock(),
        incidents=LifecycleIncidentService(),
    )
    escalated_until, count, escalated, threshold = await escalate_failure_svc.cooldown_device(
        db_session, refreshed.id, device.id, reason="still flaky", ttl_seconds=5
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
        await _failure_svc.report_preparation_failure(db_session, run.id, device.id, message="bad setup")


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
        operational_state=DeviceOperationalState.maintenance,
    )
    busy = await create_device(
        db_session,
        host_id=db_host.id,
        name="Busy Release Device",
        identity_value="run-release-busy-001",
        operational_state=DeviceOperationalState.busy,
    )
    odd = await create_device(
        db_session,
        host_id=db_host.id,
        name="Odd Release Device",
        identity_value="run-release-odd-001",
        operational_state=DeviceOperationalState.offline,
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

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    pending = await _release_svc.release_devices(
        db_session,
        run,
        commit=False,
        terminate_grid_sessions=True,
    )

    assert set(pending) == {maintenance.id, busy.id, odd.id}


async def test_release_devices_handles_missing_maintenance_and_already_restored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_id = uuid.uuid4()
    maintenance_id = uuid.uuid4()
    not_reserved_id = uuid.uuid4()
    reservations = [
        SimpleNamespace(id=uuid.uuid4(), device_id=missing_id, released_at=None),
        SimpleNamespace(id=uuid.uuid4(), device_id=maintenance_id, released_at=None),
        SimpleNamespace(id=uuid.uuid4(), device_id=not_reserved_id, released_at=None),
    ]
    run = SimpleNamespace(
        id=uuid.uuid4(),
        name="fake-release",
        state=RunState.cancelled,
        device_reservations=reservations,
    )
    db = AsyncMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(RunReleaseService, "_mark_running_sessions_released", AsyncMock())
    monkeypatch.setattr(
        f"{RUN_RELEASE_MODULE}.device_locking.lock_devices",
        AsyncMock(
            return_value=[
                # maintenance_id uses operational_state=maintenance (the new signal)
                SimpleNamespace(
                    id=maintenance_id,
                    operational_state=DeviceOperationalState.maintenance,
                ),
                # not_reserved_id: device with no active reservation (was_reserved=False) and not busy
                SimpleNamespace(id=not_reserved_id, operational_state=DeviceOperationalState.available),
            ]
        ),
    )
    # device_is_reserved is called before reservation.released_at is set;
    # patch it so maintenance_id returns True (was reserved) and not_reserved_id returns False.
    device_reservation_map = {maintenance_id: True, not_reserved_id: False}
    monkeypatch.setattr(
        f"{RUN_RELEASE_MODULE}.device_is_reserved",
        AsyncMock(side_effect=lambda _db, device_id: device_reservation_map[device_id]),
    )

    pending = await _release_svc.release_devices(db, run, commit=True)

    assert pending == [maintenance_id, not_reserved_id]
    assert all(reservation.released_at is not None for reservation in reservations)
    db.commit.assert_awaited_once()


async def test_report_preparation_failure_missing_and_terminal_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.get_run", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await _failure_svc.report_preparation_failure(AsyncMock(), uuid.uuid4(), uuid.uuid4(), message="bad")

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
        await _failure_svc.report_preparation_failure(AsyncMock(), terminal.id, uuid.uuid4(), message="bad")


async def test_run_service_small_async_branch_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_service._reserved_entry_is_excluded(
        SimpleNamespace(excluded=True, excluded_until=datetime.now(UTC) + timedelta(minutes=1))
    )
    with pytest.raises(ValueError, match="exceeds maximum"):
        allocator_fake = RunAllocatorService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {"reservations.max_ttl_minutes": 10, "reservations.default_heartbeat_timeout_sec": 30}
            ),
            circuit_breaker=_circuit_breaker,
        )
        from app.runs.schemas import RunCreate as _RunCreate

        allocator_fake._resolve_run_options(_RunCreate(name="x", requirements=[], ttl_minutes=20))

    class CountsResult:
        def all(self) -> list[tuple[uuid.UUID | None, object, int]]:
            run_id = uuid.uuid4()
            return [(None, SessionStatus.failed, 2), (run_id, "custom", 3)]

    class CountsSession:
        async def execute(self, *_args: object, **_kwargs: object) -> CountsResult:
            return CountsResult()

    counts = await _query_svc.fetch_session_counts(CountsSession(), [uuid.uuid4()])  # type: ignore[arg-type]
    assert len(counts) == 1
    assert next(iter(counts.values())).total == 3

    missing_device_id = uuid.uuid4()

    class DeferredSession:
        async def get(self, *_args: object, **_kwargs: object) -> object | None:
            return None

    await _release_svc.complete_deferred_stops_post_commit(DeferredSession(), [missing_device_id])  # type: ignore[arg-type]
    # The deferred_stop mock should not be called for a missing device
    _release_svc._deferred_stop.complete_deferred_stop_if_session_ended.assert_not_awaited()  # type: ignore[attr-defined]


async def test_clear_desired_grid_run_id_skips_released_and_missing_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    released = SimpleNamespace(device_id=uuid.uuid4(), released_at=datetime.now(UTC))
    active = SimpleNamespace(device_id=uuid.uuid4(), released_at=None)
    run = SimpleNamespace(id=uuid.uuid4(), device_reservations=[released, active])
    db = AsyncMock()
    monkeypatch.setattr(f"{RUN_RELEASE_MODULE}.device_locking.lock_device", AsyncMock(side_effect=NoResultFound))
    revoke = AsyncMock()
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", revoke)

    await _release_svc.clear_desired_grid_run_id_for_run(db, run=run, caller="run_completed")
    revoke.assert_not_awaited()


# ── Task 3.6: hold=None readers migrated to operational_state + reservation row ──


@pytest.mark.db
async def test_release_maintenance_device_uses_operational_state_not_hold(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device in operational_state=maintenance with hold=None must land in
    devices_pending_lifecycle_cleanup — not get restored to available."""
    maintenance_device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maint-no-hold",
        identity_value="run-release-maint-no-hold-001",
        operational_state=DeviceOperationalState.maintenance,
    )
    run = await create_reserved_run(
        db_session,
        name="release-maint-no-hold-run",
        devices=[maintenance_device],
        state=RunState.cancelled,
    )
    await db_session.refresh(run, attribute_names=["device_reservations"])

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    pending = await _release_svc.release_devices(db_session, run, commit=False)

    assert maintenance_device.id in pending
    # Operational state must not be overwritten — device stays in maintenance.
    assert maintenance_device.operational_state == DeviceOperationalState.maintenance


@pytest.mark.db
async def test_release_reserved_device_uses_reservation_row_not_hold(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with an active reservation row is restored via the reservation query.

    Reserved state is detected via ``device_is_reserved(db, device.id)`` rather than the
    ``hold`` column (which is no longer written). The release path restores the operational
    state and queues the device for lifecycle cleanup; it performs no hold write.
    """
    from app.devices.models import DeviceReservation

    reserved_device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-no-hold",
        identity_value="run-release-reserved-no-hold-001",
        operational_state=DeviceOperationalState.available,
    )
    run = TestRun(
        name="release-reserved-no-hold-run",
        state=RunState.cancelled,
        requirements=[{"platform_id": reserved_device.platform_id, "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()
    reservation = DeviceReservation(
        run=run,
        device_id=reserved_device.id,
        identity_value=reserved_device.identity_value,
        connection_target=reserved_device.connection_target,
        pack_id=reserved_device.pack_id,
        platform_id=reserved_device.platform_id,
        os_version=reserved_device.os_version,
        released_at=None,
    )
    db_session.add(reservation)
    await db_session.commit()
    await db_session.refresh(run, attribute_names=["device_reservations"])

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)

    pending = await _release_svc.release_devices(db_session, run, commit=False)

    # The reservation row drives the branch: the device is queued for lifecycle cleanup
    # and its operational state is restored, with no hold write.
    assert reserved_device.id in pending
