import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.pagination import encode_cursor
from app.devices.models import Device, DeviceIntent, DeviceOperationalState, DeviceReservation
from app.devices.services import state_write_guard
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import GRID_ROUTING, RECOVERY, RESERVATION, IntentRegistration
from app.devices.services.maintenance import MaintenanceService
from app.events.event_bus import EventBus
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.hosts.models import Host
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs import service as run_service
from app.runs.models import RunState, TestRun
from app.runs.schemas import DeviceRequirement
from app.runs.service_allocator import (
    RunAllocatorService,
    _find_matching_devices,
    _format_requirement_count,
    _minimum_required_count,
    _select_matching_devices,
)
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService, _resolve_session_target
from app.runs.service_query import RunQueryService
from app.runs.service_reservation import (
    RunReservationService,
    get_device_reservation_with_entry,
    run_release_intent_sources,
)
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RUN_FAILURES_MODULE = "app.runs.service_lifecycle_failures"
RUN_RELEASE_MODULE = "app.runs.service_lifecycle_release"
RUN_LOOKUP_MODULE = "app.runs.service_reservation"

_settings = FakeSettingsReader({})
_circuit_breaker = AgentCircuitBreaker(publisher=event_bus, settings=_settings)
_query_svc = RunQueryService()
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, release=_release_svc)
_failure_svc = RunFailureService(
    publisher=event_bus,
    settings=_settings,
    circuit_breaker=_circuit_breaker,
    maintenance=MaintenanceService(review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus),
    lifecycle_actions=AsyncMock(),
    reservation=RunReservationService(review=build_review_service()),
    incidents=LifecycleIncidentService(),
)


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

    listed, total = await _query_svc.list_runs(db_session)
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
    lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, release=mock_release)

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

    assert await svc.exclude_device_from_run(db_session, uuid.uuid4(), reason="missing") is None
    assert await run_service.get_device_reservation(db_session, device.id) == run
    reservation_map = await run_service.get_device_reservation_map(db_session, [device.id])
    assert reservation_map[device.id] == run
    assert run_service.get_reservation_context_for_device(None, device.id) == (None, None)
    assert run_service.get_reservation_context_for_device(run, uuid.uuid4()) == (run, None)

    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    excluded = await svc.exclude_device_from_run(db_session, device.id, reason="bad device", commit=False)
    assert excluded is not None
    assert entry.excluded is True
    same_exclusion = await svc.exclude_device_from_run(db_session, device.id, reason="bad device", commit=False)
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
    committed_excluded = await svc.exclude_device_from_run(db_session, device.id, reason="missing lock")
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
    excluded_until, count, escalated, threshold, entered_maintenance = await failure_svc.cooldown_device(
        db_session, run.id, device.id, reason="flaky", ttl_seconds=5
    )

    assert excluded_until is not None
    assert (count, escalated, threshold, entered_maintenance) == (1, False, 3, False)


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
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1,
                active_connection_target="",
            )
        )
    session = Session(session_id="release-success", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=True),
    )
    release_with_fake_grid = RunReleaseService(
        publisher=event_bus,
        settings=_settings,
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


@pytest.mark.db
async def test_mark_running_sessions_released_terminates_concurrently_across_hosts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave-5 #8: run release awaited each Appium DELETE serially (10s timeout each)
    while the callers hold the run FOR UPDATE — worst case Nx10s of wall time under
    lock. The DELETEs must overlap across hosts (bounded per host); the DB writes
    stay serial afterwards."""
    import asyncio

    other_host = Host(
        hostname=f"db-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.0.251",
        os_type=db_host.os_type,
        agent_port=5100,
        status=db_host.status,
    )
    db_session.add(other_host)
    await db_session.flush()

    devices = []
    for i, host in enumerate([db_host, db_host, other_host, other_host]):
        device = await create_device(
            db_session,
            host_id=host.id,
            name=f"Concurrent Release Device {i}",
            identity_value=f"run-release-conc-{i:03d}",
            operational_state=DeviceOperationalState.busy,
        )
        with state_write_guard.bypass():
            db_session.add(
                AppiumNode(
                    device_id=device.id,
                    port=4723 + i,
                    desired_state=AppiumDesiredState.running,
                    desired_port=4723 + i,
                    pid=1,
                    active_connection_target="",
                )
            )
        devices.append(device)
    run = await create_reserved_run(db_session, name="release-conc-run", devices=devices, state=RunState.cancelled)
    sessions = [
        Session(session_id=f"release-conc-{i}", device_id=device.id, run_id=run.id, status=SessionStatus.running)
        for i, device in enumerate(devices)
    ]
    db_session.add_all(sessions)
    await db_session.commit()

    in_flight = 0
    max_in_flight = 0

    async def fake_terminate(target: str, session_id: str, **_: object) -> bool:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return True

    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.terminate_session", fake_terminate)
    svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    await svc._mark_running_sessions_released(db_session, run, datetime.now(UTC), terminate_grid_sessions=True)

    assert max_in_flight >= 2, f"terminates did not overlap (max in flight: {max_in_flight})"
    for session in sessions:
        assert session.status == SessionStatus.error
        assert session.ended_at is not None


@pytest.mark.db
async def test_mark_running_sessions_released_expires_claimed_ticket(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: run-release closes a grid-allocated session AND terminalizes the ``claimed``
    ticket that minted it — otherwise the ticket dangles ``claimed`` until retention
    purges the session row."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Ticket Device",
        identity_value="run-release-ticket-001",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(db_session, name="release-ticket-run", devices=[device], state=RunState.cancelled)
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1,
                active_connection_target="",
            )
        )
    session = Session(
        session_id="release-ticket-sess", device_id=device.id, run_id=run.id, status=SessionStatus.running
    )
    db_session.add(session)
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body={"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}},
        status=GridQueueStatus.claimed,
        session_row_id=session.id,
    )
    db_session.add(ticket)
    await db_session.commit()

    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=True),
    )
    svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    await svc._mark_running_sessions_released(db_session, run, datetime.now(UTC), terminate_grid_sessions=True)

    assert session.status == SessionStatus.error
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired


@pytest.mark.db
async def test_mark_running_sessions_released_leaves_row_when_terminate_fails(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appium DELETE failure leaves the running row untouched (not falsely ended)."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Term Fail Device",
        identity_value="run-release-termfail-001",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(db_session, name="release-termfail-run", devices=[device], state=RunState.cancelled)
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1,
                active_connection_target="",
            )
        )
    session = Session(session_id="release-termfail", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=False),
    )
    svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    await svc._mark_running_sessions_released(db_session, run, datetime.now(UTC), terminate_grid_sessions=True)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


def test_resolve_session_target_no_device_returns_none() -> None:
    """#9 read-only target resolution: a session with no device_id, and a session whose
    device row is absent from the batch-loaded map, both resolve to no target."""
    no_device = Session(session_id="no-device-target", device_id=None, status=SessionStatus.running)
    assert _resolve_session_target(no_device, {}) is None

    missing_device = Session(session_id="missing-device-target", device_id=uuid.uuid4(), status=SessionStatus.running)
    assert _resolve_session_target(missing_device, {}) is None


@pytest.mark.db
async def test_resolve_session_target_falls_back_to_stored_router_target(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """#9: a released session whose live node target is unresolvable (node row cleared
    during recovery backoff) still resolves a target via the router_target stored at
    allocation, so the session gets terminated rather than left running."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Stale Node Port Device",
        identity_value="run-release-staleport-001",
        operational_state=DeviceOperationalState.busy,
    )
    # No AppiumNode row -> node_target() returns None; resolve_router_target must fall
    # back to the target stored at allocation.
    session = Session(
        session_id="staleport-sess",
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.99:4723",
    )
    db_session.add(session)
    await db_session.commit()

    # Mirror the caller's batch load: eager appium_node/host so the pure resolver
    # never triggers a lazy load.
    loaded = (
        (
            await db_session.execute(
                select(Device)
                .options(selectinload(Device.appium_node), selectinload(Device.host))
                .where(Device.id == device.id)
            )
        )
        .scalars()
        .one()
    )

    target = _resolve_session_target(session, {device.id: loaded})
    assert target == "http://10.0.0.99:4723"


@pytest.mark.db
async def test_mark_running_sessions_released_closes_pending_session(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """#3: a run cancelled while a grid session is still ``pending`` (allocate->confirm
    window) terminalizes the pending row — without an Appium DELETE (placeholder id) —
    so the device is not double-allocated when the router's later confirm 409s."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Pending Release Device",
        identity_value="run-release-pending-001",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(db_session, name="release-pending-run", devices=[device], state=RunState.cancelled)
    pending = Session(
        session_id=f"alloc-{uuid.uuid4()}",
        device_id=device.id,
        run_id=run.id,
        status=SessionStatus.pending,
    )
    db_session.add(pending)
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body={"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}},
        status=GridQueueStatus.claimed,
        session_row_id=pending.id,
    )
    db_session.add(ticket)
    await db_session.commit()

    svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    # The pending device must still be considered busy by the release gate (#3).
    from app.sessions import service as sessions_service

    assert await sessions_service.device_has_running_session(db_session, device.id) is True

    # C12: a never-confirmed pending row never emitted session.started, so closing it
    # must NOT emit session.ended (matching the reaper's silent close).
    queued: list[str] = []
    orig_queue = EventBus.queue_for_session

    def _spy(self: EventBus, db: object, event_type: str, data: dict, **kwargs: object) -> None:
        queued.append(event_type)
        return orig_queue(self, db, event_type, data, **kwargs)  # type: ignore[arg-type]

    with patch.object(EventBus, "queue_for_session", _spy):
        await svc._mark_running_sessions_released(db_session, run, datetime.now(UTC), terminate_grid_sessions=True)
    assert pending.status == SessionStatus.error
    assert pending.error_type == "run_released"
    assert pending.ended_at is not None
    assert "session.ended" not in queued
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired


@pytest.mark.db
async def test_mark_running_sessions_released_emits_ended_event_and_reconciles(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#12: run-release routes the session close through close_running_session so it
    emits session.ended and reconciles the device instead of stamping status inline."""
    import app.sessions.service as sessions_service

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Ended Event Device",
        identity_value="run-release-endedevent-001",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(
        db_session, name="release-endedevent-run", devices=[device], state=RunState.cancelled
    )
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1,
                active_connection_target="",
            )
        )
    session = Session(session_id="endedevent-sess", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    queued: list[str] = []
    orig_queue = EventBus.queue_for_session

    def _spy(self: EventBus, db: object, event_type: str, data: dict, **kwargs: object) -> None:
        queued.append(event_type)
        return orig_queue(self, db, event_type, data, **kwargs)  # type: ignore[arg-type]

    # Patch the class, not the shared instance: monkeypatch.setattr on an instance
    # whose attribute resolves via the class restores by re-setting an instance
    # attribute (it does not delete it), permanently shadowing later class-level
    # patches of the same shared test_event_bus and leaking across xdist workers.
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", _spy)
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=True),
    )

    reconciled: list[uuid.UUID] = []
    orig_revoke = sessions_service.IntentService.revoke_intents_and_reconcile

    async def _spy_revoke(self: object, *, device_id: uuid.UUID, **kwargs: object) -> object:
        reconciled.append(device_id)
        return await orig_revoke(self, device_id=device_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sessions_service.IntentService, "revoke_intents_and_reconcile", _spy_revoke)

    svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    await svc._mark_running_sessions_released(db_session, run, datetime.now(UTC), terminate_grid_sessions=True)

    assert session.status == SessionStatus.error
    assert session.error_type == "run_released"
    assert "session.ended" in queued
    assert device.id in reconciled


async def test_cooldown_escalation_releases_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Cooldown Escalate Device",
        identity_value="run-cooldown-esc-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="cooldown-esc-run", devices=[device], state=RunState.active)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.deliver_agent_reconfigures", AsyncMock())

    maintenance = AsyncMock()
    svc = RunFailureService(
        publisher=event_bus,
        settings=FakeSettingsReader(
            {
                "general.device_cooldown_max_sec": 60,
                "general.device_cooldown_escalation_threshold": 1,
                "general.run_failure_escalates_to_maintenance": False,
            }
        ),
        circuit_breaker=_circuit_breaker,
        maintenance=maintenance,
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=AsyncMock(),
    )

    excluded_until, count, escalated, threshold, entered_maintenance = await svc.cooldown_device(
        db_session, run.id, device.id, reason="still flaky", ttl_seconds=5
    )

    assert (excluded_until, count, escalated, threshold, entered_maintenance) == (None, 1, True, 1, False)
    # Released; maintenance toggle off -> stays available, not maintenance.
    maintenance.enter_maintenance.assert_not_awaited()
    active_run, _active = await get_device_reservation_with_entry(db_session, device.id)
    assert active_run is None


async def test_cooldown_escalation_enters_maintenance_when_enabled(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Cooldown Escalate Maint Device",
        identity_value="run-cooldown-esc-maint-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="cooldown-esc-maint-run", devices=[device], state=RunState.active)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(f"{RUN_FAILURES_MODULE}.deliver_agent_reconfigures", AsyncMock())

    maintenance = AsyncMock()
    svc = RunFailureService(
        publisher=event_bus,
        settings=FakeSettingsReader(
            {
                "general.device_cooldown_max_sec": 60,
                "general.device_cooldown_escalation_threshold": 1,
                "general.run_failure_escalates_to_maintenance": True,
            }
        ),
        circuit_breaker=_circuit_breaker,
        maintenance=maintenance,
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=AsyncMock(),
    )

    excluded_until, count, escalated, threshold, entered_maintenance = await svc.cooldown_device(
        db_session, run.id, device.id, reason="still flaky", ttl_seconds=5
    )

    assert (excluded_until, count, escalated, threshold, entered_maintenance) == (None, 1, True, 1, True)
    # Escalation entered maintenance because the toggle is on.
    maintenance.enter_maintenance.assert_awaited_once()
    # Released from the run regardless of toggle.
    active_run, _active = await get_device_reservation_with_entry(db_session, device.id)
    assert active_run is None


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


async def test_report_preparation_failure_releases_device_when_escalation_disabled(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Prep Release Device",
        identity_value="run-prep-release-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="prep-release-run", devices=[device], state=RunState.active)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())

    maintenance = AsyncMock()
    lifecycle_actions = AsyncMock()
    incidents = AsyncMock()
    svc = RunFailureService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.run_failure_escalates_to_maintenance": False}),
        circuit_breaker=_circuit_breaker,
        maintenance=maintenance,
        lifecycle_actions=lifecycle_actions,
        reservation=RunReservationService(review=build_review_service()),
        incidents=incidents,
    )

    refreshed = await svc.report_preparation_failure(db_session, run.id, device.id, message="bad setup")

    # Released from the run (not a sticky exclusion): released_at set, reason recorded, no active reservation.
    entry = next(r for r in refreshed.device_reservations if r.device_id == device.id)
    assert entry.released_at is not None
    assert entry.exclusion_reason == "bad setup"
    active_run, _active = await get_device_reservation_with_entry(db_session, device.id)
    assert active_run is None
    # No maintenance / no maintenance-coupled failure-context write.
    maintenance.enter_maintenance.assert_not_awaited()
    lifecycle_actions.record_run_escalation_failure.assert_not_awaited()
    # Incident still recorded.
    incidents.record_lifecycle_incident.assert_awaited_once()
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_report_preparation_failure_releases_and_maintains_when_enabled(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Prep Release Maint Device",
        identity_value="run-prep-release-maint-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="prep-release-maint-run", devices=[device], state=RunState.active)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())

    maintenance = AsyncMock()
    lifecycle_actions = AsyncMock()
    incidents = AsyncMock()
    svc = RunFailureService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.run_failure_escalates_to_maintenance": True}),
        circuit_breaker=_circuit_breaker,
        maintenance=maintenance,
        lifecycle_actions=lifecycle_actions,
        reservation=RunReservationService(review=build_review_service()),
        incidents=incidents,
    )

    refreshed = await svc.report_preparation_failure(db_session, run.id, device.id, message="bad setup")

    entry = next(r for r in refreshed.device_reservations if r.device_id == device.id)
    assert entry.released_at is not None
    maintenance.enter_maintenance.assert_awaited_once()
    lifecycle_actions.record_run_escalation_failure.assert_awaited_once()
    incidents.record_lifecycle_incident.assert_awaited_once()


@pytest.mark.db
async def test_report_preparation_failure_rejects_empty_message(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Prep Empty Msg Device",
        identity_value="run-prep-empty-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="prep-empty-run", devices=[device], state=RunState.active)
    with pytest.raises(ValueError, match="message is required"):
        await _failure_svc.report_preparation_failure(db_session, run.id, device.id, message="  ")


@pytest.mark.db
async def test_release_device_from_run_releases_and_frees_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Primitive Device",
        identity_value="run-release-prim-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="release-prim-run", devices=[device], state=RunState.active)

    returned = await RunReservationService(review=build_review_service()).release_device_from_run(
        db_session, device.id, reason="CI preparation failed", publisher=event_bus, commit=True
    )

    assert returned is not None and returned.id == run.id
    # The reservation is released, with the reason recorded for run history.
    entry = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    assert entry.released_at is not None
    assert entry.exclusion_reason == "CI preparation failed"
    # No active reservation remains -> device is free for other runs and invisible to the self-heal loop.
    active_run, active_entry = await get_device_reservation_with_entry(db_session, device.id)
    assert active_run is None and active_entry is None


@pytest.mark.db
async def test_release_device_from_run_no_excluded_flag_and_full_intent_revoke(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Release Full Revoke Device",
        identity_value="run-release-full-001",
        operational_state=DeviceOperationalState.available,
    )
    run = await create_reserved_run(db_session, name="release-full-run", devices=[device], state=RunState.active)
    # Seed all five intent sources that release_device_from_run must revoke.
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="seed full intent set",
        intents=[
            IntentRegistration(
                source=f"run:{run.id}",
                axis=GRID_ROUTING,
                run_id=run.id,
                payload={"accepting_new_sessions": True, "priority": 10},
            ),
            IntentRegistration(
                source=f"cooldown:grid:{run.id}",
                axis=GRID_ROUTING,
                run_id=run.id,
                payload={"accepting_new_sessions": False, "priority": 50},
            ),
            IntentRegistration(
                source=f"cooldown:reservation:{run.id}",
                axis=RESERVATION,
                run_id=run.id,
                payload={"excluded": True, "priority": 50, "exclusion_reason": "flaky"},
            ),
            IntentRegistration(
                source=f"cooldown:recovery:{run.id}",
                axis=RECOVERY,
                run_id=run.id,
                payload={"allowed": False, "priority": 50, "reason": "flaky"},
            ),
            IntentRegistration(
                source=f"health_failure:reservation:{device.id}",
                axis=RESERVATION,
                run_id=run.id,
                payload={"excluded": True, "priority": 60, "exclusion_reason": "bad checks"},
            ),
        ],
    )
    await db_session.commit()

    await RunReservationService(review=build_review_service()).release_device_from_run(
        db_session, device.id, reason="CI preparation failed", publisher=event_bus, commit=True
    )

    # #11: released row must NOT be excluded (invariant: not (released_at and excluded)).
    entry = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    assert entry.released_at is not None
    assert entry.excluded is False
    assert entry.exclusion_reason == "CI preparation failed"
    # #2/#7: the full intent set is gone (no cooldown:* or health_failure:reservation lingering).
    remaining = (
        (await db_session.execute(select(DeviceIntent.source).where(DeviceIntent.device_id == device.id)))
        .scalars()
        .all()
    )
    assert not any(
        s.startswith("cooldown:") or s.startswith("health_failure:") or s.startswith("run:") for s in remaining
    )


@pytest.mark.db
async def test_reserved_device_info_exposes_released_at(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="DTO Released Device",
        identity_value="run-dto-released-001",
        operational_state=DeviceOperationalState.available,
    )
    await create_reserved_run(db_session, name="dto-released-run", devices=[device], state=RunState.active)
    await RunReservationService(review=build_review_service()).release_device_from_run(
        db_session, device.id, reason="CI preparation failed", publisher=event_bus, commit=True
    )

    entry = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    info = entry.to_reserved_device_info()
    assert info["released_at"] is not None  # released device is distinguishable
    assert info["excluded"] is False  # not a restorable exclusion (depends on Task 1)
    assert info["exclusion_reason"] == "CI preparation failed"


def test_run_release_intent_sources_lists_the_full_set() -> None:
    import uuid as _uuid

    run_id = _uuid.UUID("11111111-1111-1111-1111-111111111111")
    device_id = _uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert run_release_intent_sources(run_id, device_id) == [
        f"run:{run_id}",
        f"cooldown:grid:{run_id}",
        f"cooldown:reservation:{run_id}",
        f"cooldown:recovery:{run_id}",
        f"health_failure:reservation:{device_id}",
    ]


@pytest.mark.db
async def test_release_device_from_run_clears_prior_exclusion(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: release_device_from_run must clear a pre-existing exclusion.

    When a sub-threshold cooldown sets excluded=True + a future excluded_until window
    and the escalation threshold is then crossed, release_device_from_run is called.
    The released row must not carry excluded=True or a live excluded_window — otherwise
    the GiST ExcludeConstraint collides on the next reservation for this device.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Prior Exclusion Device",
        identity_value="run-release-prior-excl-001",
        operational_state=DeviceOperationalState.available,
    )
    await create_reserved_run(db_session, name="release-prior-excl-run", devices=[device], state=RunState.active)

    # Seed the reservation as already-excluded (mirrors the sub-threshold cooldown path).
    entry = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one()
    entry.excluded = True
    entry.excluded_at = datetime.now(UTC)
    entry.excluded_until = datetime.now(UTC) + timedelta(hours=1)
    await db_session.commit()

    await RunReservationService(review=build_review_service()).release_device_from_run(
        db_session, device.id, reason="threshold crossed", publisher=event_bus, commit=True
    )

    released = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    assert released.released_at is not None
    assert released.excluded is False
    assert released.excluded_at is None
    assert released.excluded_until is None
