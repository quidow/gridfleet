from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_policy import LifecyclePolicyService
from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
from app.devices.services.state import DeviceStateService
from app.hosts.models import Host
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from app.sessions.protocols import SessionCrudProtocol
from app.sessions.service import SessionCrudService
from tests.helpers import create_device_record, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

from tests.fakes import FakeSettingsReader  # noqa: E402


def _make_real_lifecycle(publisher: object = None) -> LifecyclePolicyService:
    """Return a real LifecyclePolicyService for tests that need actual DB mutations."""
    pub = publisher if publisher is not None else event_bus
    return LifecyclePolicyService(
        publisher=pub,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(publisher=pub, reservation=RunReservationService()),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


async def test_update_session_status_restores_busy_device_when_last_session_finishes(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="android-stale-busy",
        connection_target="android-stale-busy",
        name="Android stale-busy",
        os_version="14",
        operational_state="busy",
    )

    session = Session(session_id="android-sess-1", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    crud = SessionCrudService(
        publisher=Mock(), device_state=DeviceStateService(publisher=Mock()), lifecycle=AsyncMock()
    )
    updated = await crud.update_session_status(db_session, "android-sess-1", SessionStatus.passed)

    assert updated is not None
    assert updated.status == SessionStatus.passed
    assert updated.ended_at is not None

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_update_session_status_preserves_busy_when_another_session_is_running(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="busy-multi-session",
        connection_target="busy-multi-session",
        name="Busy Multi Session",
        os_version="14",
        operational_state="busy",
    )

    db_session.add_all(
        [
            Session(session_id="sess-a", device_id=device.id, status=SessionStatus.running),
            Session(session_id="sess-b", device_id=device.id, status=SessionStatus.running),
        ]
    )
    await db_session.commit()

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    updated = await crud.update_session_status(db_session, "sess-a", SessionStatus.failed)

    assert updated is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_update_session_status_restores_reserved_when_active_run_owns_device(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="reserved-session-device",
        connection_target="reserved-session-device",
        name="Reserved Session Device",
        os_version="14",
        operational_state="busy",
    )
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    await create_reserved_run(db_session, name="Reserved Session Run", devices=[device])

    session = Session(session_id="reserved-sess", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    crud = SessionCrudService(
        publisher=Mock(), device_state=DeviceStateService(publisher=Mock()), lifecycle=AsyncMock()
    )
    updated = await crud.update_session_status(db_session, "reserved-sess", SessionStatus.error)

    assert updated is not None
    await db_session.refresh(device)
    active_reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert active_reservation is not None

    result = await db_session.execute(select(Session).where(Session.session_id == "reserved-sess"))
    stored = result.scalar_one()
    assert stored.ended_at is not None


async def test_update_session_status_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-stuck-stop-1",
            connection_target="policy-stuck-stop-1",
            name="Stuck Deferred Stop Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-1",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    crud = SessionCrudService(
        publisher=Mock(), device_state=DeviceStateService(publisher=Mock()), lifecycle=_make_real_lifecycle()
    )
    updated = await crud.update_session_status(db_session, "sess-stuck-stop-1", SessionStatus.passed)
    assert updated is not None
    assert updated.ended_at is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "update_session_status must clear stop_pending after the last session ends"
    )


async def test_register_session_does_not_attach_run_id_when_run_is_preparing(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.runs.models import RunState
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="prep-session-device",
        connection_target="prep-session-device",
        name="Prep Session Device",
        os_version="14",
        operational_state="available",
    )
    device.verified_at = datetime.now(UTC)
    await db_session.commit()
    run = await create_reserved_run(db_session, name="Prep Phase Run", devices=[device], state=RunState.preparing)

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    registered = await crud.register_session(
        db_session,
        session_id="prep-session-1",
        test_name="prep-warmup",
        device_id=device.id,
    )

    assert registered.run_id is None
    await db_session.refresh(run)
    assert run.state == RunState.preparing
    assert run.started_at is None


async def test_register_session_attaches_run_id_when_run_is_active(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.runs.models import RunState
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="active-session-device",
        connection_target="active-session-device",
        name="Active Session Device",
        os_version="14",
        operational_state="available",
    )
    device.verified_at = datetime.now(UTC)
    await db_session.commit()
    run = await create_reserved_run(db_session, name="Active Phase Run", devices=[device], state=RunState.active)

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    registered = await crud.register_session(
        db_session,
        session_id="active-session-1",
        test_name="real-test",
        device_id=device.id,
    )

    assert registered.run_id == run.id
    await db_session.refresh(run)
    assert run.state == RunState.active


async def test_register_session_with_terminal_status_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-stuck-stop-2",
            connection_target="policy-stuck-stop-2",
            name="Stuck Deferred Stop Device 2",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    running = Session(
        session_id="sess-stuck-stop-2-running",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(running)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    # Simulate the running session being closed out-of-band, then a fresh terminal-status
    # registration arriving via testkit's error-session reporting path.
    running.status = SessionStatus.error
    running.ended_at = datetime.now(UTC)
    await db_session.commit()

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=_make_real_lifecycle()
    )
    await crud.register_session(
        db_session,
        session_id="sess-stuck-stop-2-error",
        test_name="error-session",
        device_id=device.id,
        status=SessionStatus.error,
        error_type="driver_init_failed",
        error_message="boom",
    )

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_update_session_status_clears_stop_pending_on_non_busy_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Non-busy availability must not gate deferred-stop cleanup.

    A concurrent operator action (or background loop) can move the device
    out of ``busy`` while the running Session row still exists. When that
    session is patched terminal, ``update_session_status`` must still run the
    lifecycle helper so a stale ``stop_pending`` does not survive the
    session-end. The previous gate on ``availability == busy`` skipped this
    case (see audit P1 / CodeAnt comment on PR 64).
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-stuck-stop-non-busy",
            connection_target="policy-stuck-stop-non-busy",
            name="Stuck Stop Non-Busy",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-non-busy",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB hung"
    )
    assert result == "deferred"

    # Simulate an operator (or another loop) flipping the device into
    # maintenance while the session row is still ``running``.
    await db_session.refresh(device)
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.maintenance
    await db_session.commit()

    crud = SessionCrudService(
        publisher=Mock(), device_state=DeviceStateService(publisher=Mock()), lifecycle=_make_real_lifecycle()
    )
    updated = await crud.update_session_status(db_session, "sess-stuck-stop-non-busy", SessionStatus.passed)
    assert updated is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "update_session_status must clear stop_pending even when device availability is no longer busy"
    )


async def test_register_session_running_returns_existing_on_conflict(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent registrants for the same ``session_id`` must converge.

    Stubs the ``get_session`` pre-check so the second call cannot
    short-circuit before reaching ``INSERT ... ON CONFLICT DO NOTHING``.
    Without the conflict-handling branch the second call would either
    raise ``IntegrityError`` or persist a duplicate ``running`` row.
    """
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="android-conflict",
        connection_target="conflict-target",
        name="Conflict Phone",
    )
    await db_session.commit()

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    first = await crud.register_session(
        db_session,
        session_id="sess-conflict",
        test_name="first",
        device_id=device.id,
        connection_target="conflict-target",
    )
    assert first.test_name == "first"

    real_get_session = crud.get_session
    pre_check_calls = {"n": 0}

    async def patched_get_session(db: AsyncSession, sid: str) -> Session | None:
        pre_check_calls["n"] += 1
        # Bypass only the pre-check (first call). The post-conflict refetch
        # (second call) must see the real winner row.
        if pre_check_calls["n"] == 1:
            return None
        return await real_get_session(db, sid)

    monkeypatch.setattr(crud, "get_session", patched_get_session)

    second = await crud.register_session(
        db_session,
        session_id="sess-conflict",
        test_name="second",
        device_id=device.id,
        connection_target="conflict-target",
    )
    # Conflict short-circuit returns the winner's row; loser's metadata is
    # discarded.
    assert second.id == first.id
    assert second.test_name == "first"
    assert pre_check_calls["n"] >= 2, "post-conflict refetch must run"

    rows = (await db_session.execute(select(Session).where(Session.session_id == "sess-conflict"))).scalars().all()
    assert len(rows) == 1


async def test_update_session_status_does_not_flap_offline_on_session_end(
    db_session: AsyncSession,
    default_host_id: str,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Regression: session-end must not emit offline with reason "Session ended".

    After Task 10 (reconciler-authoritative), session-end calls
    mark_dirty_and_reconcile which derives the correct state from durable facts.
    A node with health_running=False is not available, so the reconciler derives
    offline — but the event reason must NOT be "Session ended" (the old flap
    reason). The reconciler uses an observation-based reason (e.g. "auto_stopped").
    """
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="flap-repro",
        connection_target="flap-repro",
        name="Flap Repro",
        os_version="14",
        operational_state="busy",
    )
    device.verified_at = datetime.now(UTC)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4730,
            grid_url="http://hub.invalid:4444",
            pid=12345,
            active_connection_target=device.connection_target,
            desired_state=AppiumDesiredState.running,
            desired_port=4730,
            health_running=False,
            health_state="error",
        )
    db_session.add(node)
    db_session.add(Session(session_id="flap-sess", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    event_bus_capture.clear()

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    updated = await crud.update_session_status(db_session, "flap-sess", SessionStatus.passed)
    await settle_after_commit_tasks()

    assert updated is not None
    op_events = [
        payload
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed" and payload["device_id"] == str(device.id)
    ]
    spurious_offline = [
        p for p in op_events if p["new_operational_state"] == "offline" and p.get("reason") == "Session ended"
    ]
    assert spurious_offline == [], (
        "session-end must not emit offline with reason 'Session ended'; "
        f"got spurious offline event(s) {spurious_offline}"
    )
    # Reconciler-authoritative: health_running=False → offline is correct.
    assert len(op_events) == 1, f"expected single busy→offline transition, got {op_events}"
    assert op_events[0]["old_operational_state"] == "busy"
    assert op_events[0]["new_operational_state"] == "offline"

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


async def test_update_session_status_emits_single_offline_when_stop_in_flight(
    db_session: AsyncSession,
    default_host_id: str,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Regression: stop-in-flight session-end must NOT pass through ``available``.

    With an active graceful-stop intent flagged on the AppiumNode
    (``stop_pending=True``), the session ending must take the device directly
    from busy to offline via a single AUTO_STOP_EXECUTED transition. Routing
    through SESSION_ENDED first (busy→available) and then AUTO_STOP_EXECUTED
    (available→offline) produces two ``device.operational_state_changed``
    events back-to-back and a phantom ``available`` snapshot — operators see
    a wasted transition pair and the SESSION_ENDED state-machine event fires
    against a session that ended into an unhealthy outcome, not a clean
    busy→available restore.
    """
    from app.devices.models import DeviceIntent
    from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_HEALTH_FAILURE

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="stop-inflight-repro",
        connection_target="stop-inflight-repro",
        name="Stop Inflight Repro",
        os_version="14",
        operational_state="busy",
    )
    device.verified_at = datetime.now(UTC)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4731,
            grid_url="http://hub.invalid:4444",
            pid=23456,
            active_connection_target=device.connection_target,
            desired_state=AppiumDesiredState.running,
            desired_port=4731,
            stop_pending=True,
        )
    db_session.add(node)
    db_session.add(Session(session_id="stop-inflight-sess", device_id=device.id, status=SessionStatus.running))
    # Realistic shape: a graceful-stop intent registered by
    # ``handle_health_failure`` during the session. While the session is
    # active, intent_reconciler holds the node at desired_state=running with
    # stop_pending=True (universal session-safety downgrade). When the
    # session ends, the active_session intent is revoked and reconcile picks
    # the stop intent as the winner, taking the node to desired_state=stopped.
    db_session.add(
        DeviceIntent(
            device_id=device.id,
            source=f"health_failure:node:{device.id}",
            axis=NODE_PROCESS,
            payload={
                "action": "stop",
                "priority": PRIORITY_HEALTH_FAILURE,
                "stop_mode": "graceful",
            },
        )
    )
    await db_session.commit()
    event_bus_capture.clear()

    crud = SessionCrudService(
        publisher=event_bus, device_state=DeviceStateService(publisher=event_bus), lifecycle=AsyncMock()
    )
    updated = await crud.update_session_status(db_session, "stop-inflight-sess", SessionStatus.passed)
    await settle_after_commit_tasks()

    assert updated is not None
    op_events = [
        payload
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed" and payload["device_id"] == str(device.id)
    ]
    assert len(op_events) == 1, (
        f"stop-in-flight session-end must emit exactly one transition (busy→offline); got {op_events}"
    )
    assert op_events[0]["old_operational_state"] == "busy"
    assert op_events[0]["new_operational_state"] == "offline"
    # After Task 10: reason is derived by the reconciler (auto_stopped signal).
    assert op_events[0]["reason"] in ("Session ended with pending node stop", "auto_stopped")

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


def test_session_crud_service_satisfies_protocol() -> None:
    assert issubclass(SessionCrudService, SessionCrudProtocol)
