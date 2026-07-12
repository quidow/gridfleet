from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceEvent,
    DeviceEventType,
    DeviceOperationalState,
    DeviceReservation,
    DeviceType,
)
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService, _session_ended_severity
from tests.fakes import build_review_service
from tests.helpers import create_device_record, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

from tests.fakes import FakeSettingsReader  # noqa: E402

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _make_real_lifecycle(publisher: object = None) -> LifecyclePolicyService:
    """Return a real LifecyclePolicyService for tests that need actual DB mutations."""
    pub = publisher if publisher is not None else event_bus
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=pub,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=pub,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
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

    crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
    updated = await crud.update_session_status(db_session, "android-sess-1", SessionStatus.passed)

    assert updated is not None
    assert updated.status == SessionStatus.passed
    assert updated.ended_at is not None

    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.available


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

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    updated = await crud.update_session_status(db_session, "sess-a", SessionStatus.failed)

    assert updated is not None
    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.busy


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

    crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
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


async def test_update_session_status_clears_deferred_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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
    assert device.lifecycle_policy_state["deferred_stop"] is True

    crud = SessionCrudService(publisher=Mock(), lifecycle=_make_real_lifecycle())
    updated = await crud.update_session_status(db_session, "sess-stuck-stop-1", SessionStatus.passed)
    assert updated is not None
    assert updated.ended_at is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["deferred_stop"] is False, (
        "update_session_status must clear deferred_stop after the last session ends"
    )


async def test_update_session_status_clears_deferred_stop_on_non_busy_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Non-busy availability must not gate deferred-stop cleanup.

    A concurrent operator action (or background loop) can move the device
    out of ``busy`` while the running Session row still exists. When that
    session is patched terminal, ``update_session_status`` must still run the
    lifecycle helper so a stale ``deferred_stop`` does not survive the
    session-end. The previous gate on ``availability == busy`` skipped this
    case (see audit P1 / CodeAnt comment on PR 64).
    """
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
    device.operational_state_last_emitted = DeviceOperationalState.maintenance
    await db_session.commit()

    crud = SessionCrudService(publisher=Mock(), lifecycle=_make_real_lifecycle())
    updated = await crud.update_session_status(db_session, "sess-stuck-stop-non-busy", SessionStatus.passed)
    assert updated is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["deferred_stop"] is False, (
        "update_session_status must clear deferred_stop even when device availability is no longer busy"
    )


async def test_update_session_status_does_not_flap_offline_on_session_end(
    db_session: AsyncSession,
    default_host_id: str,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Regression: session-end must not emit offline with reason "Session ended".

    After Task 10 (reconciler-authoritative), session-end calls
    reconcile_now which derives the correct state from durable facts.
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
    node = AppiumNode(
        device_id=device.id,
        port=4730,
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

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
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
    assert device.operational_state_last_emitted == DeviceOperationalState.offline


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
    from app.lifecycle.services import remediation_log

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
    node = AppiumNode(
        device_id=device.id,
        port=4731,
        pid=23456,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
        stop_pending=True,
    )
    db_session.add(node)
    db_session.add(Session(session_id="stop-inflight-sess", device_id=device.id, status=SessionStatus.running))
    # Realistic shape: a graceful-stop directive commissioned by
    # ``handle_health_failure`` during the session. While the session is
    # active, intent_reconciler holds the node at desired_state=running with
    # stop_pending=True (universal session-safety downgrade). When the
    # session ends, the active_session intent is revoked and reconcile picks
    # the stop intent as the winner, taking the node to desired_state=stopped.
    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="session ended",
    )
    await db_session.commit()
    event_bus_capture.clear()

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
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
    assert "reason" not in op_events[0]

    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.offline


async def test_device_has_running_session_counts_pending(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    # #2: a device whose only session is a grid ``pending`` row (the allocate->confirm
    # window) is already claimed and must gate allocation-class actions the same as a
    # running session, so verification cannot start a probe on an allocated device.
    from app.sessions.service import device_has_running_session

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dhrs-pending",
        connection_target="dhrs-pending",
        name="Pending gate device",
        os_version="14",
        operational_state="busy",
    )
    db_session.add(
        Session(session_id=f"alloc-{datetime.now(UTC).timestamp()}", device_id=device.id, status=SessionStatus.pending)
    )
    await db_session.commit()
    assert await device_has_running_session(db_session, device.id) is True


async def test_update_session_status_records_no_session_ended_event(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Session end no longer writes a session_ended DeviceEvent — the Session row
    (started_at/ended_at) is the durable record and the session.ended bus event covers
    real-time consumers (plan 4b behavior change #2)."""
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="evt-end",
        connection_target="evt-end",
        name="evt-end",
        os_version="14",
        operational_state="busy",
    )
    session = Session(session_id="evt-sess-end", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    updated = await crud.update_session_status(db_session, "evt-sess-end", SessionStatus.passed)
    assert updated is not None

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.session_ended,
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == []


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (SessionStatus.passed, None, "success"),
        (SessionStatus.error, "appium_crash", "critical"),
        (SessionStatus.failed, None, "warning"),
    ],
)
def test_session_ended_severity_maps_outcome_to_severity(
    status: SessionStatus, error_type: str | None, expected: str
) -> None:
    assert _session_ended_severity(str(status), error_type) == expected
