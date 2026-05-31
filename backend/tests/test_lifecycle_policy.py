import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceEvent,
    DeviceEventType,
    DeviceHold,
    DeviceIntent,
    DeviceOperationalState,
    DeviceType,
)
from app.devices.services import health as device_health
from app.devices.services import lifecycle_policy as lifecycle_policy_module
from app.devices.services import state_write_guard
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_HEALTH_FAILURE, RECOVERY, IntentRegistration
from app.devices.services.lifecycle_policy import DeferredStopOutcome, LifecyclePolicyService
from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
from app.devices.services.lifecycle_policy_summary import (
    build_lifecycle_policy,
    build_lifecycle_policy_summary,
)
from app.hosts.models import Host
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_svc(
    publisher: object = None,
    settings: object = None,
    viability: object = None,
    node_manager: object = None,
) -> LifecyclePolicyService:
    from unittest.mock import AsyncMock, Mock

    pub = publisher if publisher is not None else Mock()
    svc_settings = settings if settings is not None else FakeSettingsReader({})
    via = viability if viability is not None else AsyncMock()
    nm = node_manager if node_manager is not None else AsyncMock()
    return LifecyclePolicyService(
        publisher=pub,  # type: ignore[arg-type]
        settings=svc_settings,  # type: ignore[arg-type]
        actions=LifecyclePolicyActionsService(publisher=pub, reservation=RunReservationService()),  # type: ignore[arg-type]
        viability=via,  # type: ignore[arg-type]
        node_manager=nm,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _speed_up_recovery_probe_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_JITTER_MAX_SEC", 0, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0, raising=False)


async def _mark_device_available(
    db: AsyncSession,
    *,
    device_id: object,
    intents: object,
    reason: str,
    **kwargs: object,
) -> None:
    del intents, reason, kwargs
    device = await db.get(Device, device_id)
    assert device is not None
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.available


async def _event_types_for_device(db_session: AsyncSession, device_id: object) -> list[DeviceEventType]:
    result = await db_session.execute(
        select(DeviceEvent.event_type).where(DeviceEvent.device_id == device_id).order_by(DeviceEvent.created_at.asc())
    )
    return list(result.scalars().all())


async def test_idle_health_failure_stops_device(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-idle-1",
            connection_target="policy-idle-1",
            name="Idle Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    result = await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )

    await db_session.refresh(device)
    assert result == "stopped"
    assert device.operational_state == DeviceOperationalState.offline
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_failure_reason"] == "ADB not responsive"
    assert policy["last_action"] == "auto_stopped"


async def test_active_session_failure_defers_stop(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-busy-1",
            connection_target="policy-busy-1",
            name="Busy Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    db_session.add(Session(session_id="sess-policy-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    result = await _make_svc(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )

    await db_session.refresh(device)
    assert result == "deferred"
    assert device.operational_state == DeviceOperationalState.busy
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["stop_pending"] is True
    assert policy["recovery_state"] == "waiting_for_session_end"
    assert await _event_types_for_device(db_session, device.id) == [DeviceEventType.lifecycle_deferred_stop]


async def test_reserved_idle_failure_excludes_run(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-run-1",
            connection_target="policy-run-1",
            name="Reserved Device",
            os_version="14",
            host_id=db_host.id,
            hold=DeviceHold.reserved,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Active Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    await _make_svc(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="Health probe failed"
    )

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    assert run.reserved_devices[0]["exclusion_reason"] == "Health probe failed"
    assert run.device_reservations[0].excluded is True
    assert run.device_reservations[0].exclusion_reason == "Health probe failed"


async def test_session_finish_completes_deferred_stop_and_excludes_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-run-2",
            connection_target="policy-run-2",
            name="Deferred Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Deferred Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-policy-2", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="Health probe failed"
    )
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, device)

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["stop_pending"] is False
    assert policy["excluded_from_run"] is True


async def test_recovery_is_suppressed_during_backoff(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-recover-2",
            connection_target="policy-recover-2",
            name="Backoff Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    with state_write_guard.bypass():
        device.lifecycle_policy_state = {
            **(device.lifecycle_policy_state or {}),
            "backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
    await db_session.commit()

    recovered = await _make_svc(publisher=event_bus).attempt_auto_recovery(
        db_session,
        device,
        source="device_checks",
        reason="Healthy again",
    )

    assert recovered is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] == "backoff"


async def test_successful_recovery_rejoins_run(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-recover-3",
            connection_target="policy-recover-3",
            name="Recovering Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Recovering Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "Health probe failed",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    register_recovery = AsyncMock(side_effect=_mark_device_available)
    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(publisher=Mock(), viability=viability)
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovered = await svc.attempt_auto_recovery(
            db_session,
            device,
            source="device_checks",
            reason="Healthy again",
        )

    await db_session.refresh(run, ["device_reservations"])
    await db_session.refresh(device)
    assert recovered is True
    assert device.hold == DeviceHold.reserved
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is False
    assert run.device_reservations[0].excluded is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "auto_recovered"
    assert policy["excluded_from_run"] is False
    event_types = await _event_types_for_device(db_session, device.id)
    assert DeviceEventType.lifecycle_recovered in event_types


@pytest.mark.db
async def test_auto_recovery_revokes_stale_health_failure_intents(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-recover-stale-intents",
            connection_target="policy-recover-stale-intents",
            name="Recovering Stale Intent Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://grid:4444"))
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="health failure",
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": PRIORITY_HEALTH_FAILURE, "stop_mode": "graceful"},
            ),
            IntentRegistration(
                source=f"health_failure:recovery:{device.id}",
                axis=RECOVERY,
                payload={"allowed": False, "priority": PRIORITY_HEALTH_FAILURE, "reason": "Node health failure"},
            ),
        ],
    )
    await db_session.commit()

    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    recovered = await _make_svc(publisher=event_bus, viability=viability).attempt_auto_recovery(
        db_session,
        device,
        source="device_checks",
        reason="Healthy again",
    )

    assert recovered is True
    sources = set(
        (await db_session.execute(select(DeviceIntent.source).where(DeviceIntent.source.like(f"%:{device.id}"))))
        .scalars()
        .all()
    )
    assert f"health_failure:node:{device.id}" not in sources
    assert f"health_failure:recovery:{device.id}" not in sources
    assert f"auto_recovery:node:{device.id}" in sources
    assert f"auto_recovery:recovery:{device.id}" in sources


@pytest.mark.db
async def test_auto_recovery_registers_node_running_precondition_on_intents(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Both auto_recovery:* sibling intents must carry the node_running precondition.

    Without it, the rows persist until the next recovery cycle overwrites them.
    With it, the precondition sweep auto-retires them once the node is observed running.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="auto-recovery-precondition",
            connection_target="auto-recovery-precondition",
            name="Auto Recovery Precondition Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://grid:4444"))
    await db_session.commit()

    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    recovered = await _make_svc(publisher=event_bus, viability=viability).attempt_auto_recovery(
        db_session,
        device,
        source="device_checks",
        reason="Healthy again",
    )

    assert recovered is True
    rows = (
        (
            await db_session.execute(
                select(DeviceIntent).where(
                    DeviceIntent.device_id == device.id,
                    DeviceIntent.source.in_([f"auto_recovery:node:{device.id}", f"auto_recovery:recovery:{device.id}"]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, f"expected both auto_recovery siblings, got {[r.source for r in rows]}"
    expected_precondition = {
        "kind": "node_running",
        "device_id": str(device.id),
        "expected": False,
    }
    for row in rows:
        assert row.precondition == expected_precondition, (
            f"{row.source} missing node_running precondition: {row.precondition!r}"
        )


async def test_recovery_rejoin_publishes_availability_event(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    async def fake_publish(name: str, payload: dict[str, object], *, severity: object = None) -> None:
        captured.append((name, payload))

    from tests.helpers import test_event_bus as event_bus

    monkeypatch.setattr(event_bus, "publish", fake_publish)

    from app.devices.services import state as state_mod

    _orig_set_hold = state_mod.set_hold

    async def _wrapped_set_hold(device: object, new_hold: object, **kwargs: object) -> object:
        if kwargs.get("publisher") is None:
            kwargs["publisher"] = event_bus
        return await _orig_set_hold(device, new_hold, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.devices.services.lifecycle_policy.set_hold", _wrapped_set_hold)

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-recover-event",
            connection_target="policy-recover-event",
            name="Recovering Device Event",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Recovering Run Event",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "Health probe failed",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    register_recovery = AsyncMock(side_effect=_mark_device_available)
    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(publisher=event_bus, viability=viability)
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovered = await svc.attempt_auto_recovery(
            db_session,
            device,
            source="device_checks",
            reason="Healthy again",
        )

    assert recovered is True
    hold_events = [payload for name, payload in captured if name == "device.hold_changed"]
    assert hold_events, "Recovery rejoin must publish hold_changed"
    rejoin_events = [p for p in hold_events if p.get("new_hold") == "reserved"]
    assert rejoin_events, f"Expected a 'reserved' transition; got: {hold_events}"
    assert "Rejoined run" in str(rejoin_events[0].get("reason"))


async def test_recovery_reloads_device_before_starting_node(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-race-1",
            connection_target="policy-race-1",
            name="Race Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    async with db_session_maker() as other_session:
        current = await other_session.get(Device, device.id)
        assert current is not None
        with state_write_guard.bypass():
            current.operational_state = DeviceOperationalState.available
        with state_write_guard.bypass():
            other_session.add(
                AppiumNode(
                    device_id=device.id,
                    port=4724,
                    grid_url="http://grid:4444",
                    pid=1234,
                    active_connection_target=device.connection_target,
                    desired_state=AppiumDesiredState.running,
                    desired_port=4724,
                )
            )
        await other_session.commit()

    register_recovery = AsyncMock()
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovered = await _make_svc(publisher=event_bus).attempt_auto_recovery(
            db_session,
            device,
            source="device_checks",
            reason="Healthy again",
        )

    assert recovered is False
    register_recovery.assert_not_awaited()


async def test_failed_recovery_sets_backoff_and_keeps_exclusion(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-recover-4",
            connection_target="policy-recover-4",
            name="Flaky Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Flaky Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "Health probe failed",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    register_recovery = AsyncMock(side_effect=_mark_device_available)
    probe_mock = AsyncMock(
        return_value={
            "status": "failed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": None,
            "error": "Session create failed",
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(publisher=Mock(), viability=viability)
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovered = await svc.attempt_auto_recovery(
            db_session,
            device,
            source="device_checks",
            reason="Healthy again",
        )

    await db_session.refresh(run, ["device_reservations"])
    await db_session.refresh(device)
    assert recovered is False
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    assert run.device_reservations[0].excluded is True
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "recovery_failed"
    assert policy["backoff_until"] is not None
    event_types = await _event_types_for_device(db_session, device.id)
    assert DeviceEventType.lifecycle_recovery_failed in event_types
    assert DeviceEventType.lifecycle_recovery_backoff in event_types


async def test_recovery_retries_transient_probe_failure_before_stopping_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-retry-1",
            connection_target="policy-retry-1",
            name="Retry Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.virtual,
        )
    db_session.add(device)
    await db_session.commit()

    register_recovery = AsyncMock(side_effect=_mark_device_available)
    mock_probe = AsyncMock(
        side_effect=[
            {
                "status": "failed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": None,
                "error": "Android settings service is not ready",
                "checked_by": "recovery",
            },
            {
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ]
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = mock_probe
    svc = _make_svc(publisher=event_bus, viability=viability)
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovered = await svc.attempt_auto_recovery(
            db_session,
            device,
            source="device_checks",
            reason="Healthy again",
        )

    await db_session.refresh(device)
    assert recovered is True
    assert device.operational_state == DeviceOperationalState.available
    assert mock_probe.await_count == 2
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "auto_recovered"
    assert policy["backoff_until"] is None


async def test_deferred_stop_survives_restart_boundary(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-restart-1",
            connection_target="policy-restart-1",
            name="Restart Deferred Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    session = Session(session_id="sess-policy-restart", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    result = await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)

    await db_session.refresh(reloaded)
    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert reloaded.operational_state == DeviceOperationalState.offline
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_failed_recovery_backoff_survives_restart_and_uses_settings(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-restart-2",
            connection_target="policy-restart-2",
            name="Restart Backoff Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    register_recovery = AsyncMock(side_effect=_mark_device_available)
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 5,
            "general.lifecycle_recovery_backoff_max_sec": 20,
            "general.lifecycle_recovery_review_threshold": 5,
            "appium.port_range_start": 4720,
            "appium.port_range_end": 4800,
            "grid.hub_url": "http://hub:4444",
        }
    )
    probe_mock = AsyncMock(
        return_value={
            "status": "failed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": None,
            "error": "Probe failed",
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    with patch("app.devices.services.lifecycle_policy.register_intents_and_reconcile", new=register_recovery):
        recovery_started_at = datetime.now(UTC)
        recovered = await _make_svc(publisher=Mock(), settings=settings, viability=viability).attempt_auto_recovery(
            db_session, device, source="device_checks", reason="Healthy again"
        )

    assert recovered is False
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    backoff_until = datetime.fromisoformat(device.lifecycle_policy_state["backoff_until"])
    assert 5 <= (backoff_until - recovery_started_at).total_seconds() <= 8

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    policy = await build_lifecycle_policy(db_session, reloaded)
    assert device.lifecycle_policy_state is not None
    assert policy["recovery_state"] == "backoff"
    assert policy["backoff_until"] == device.lifecycle_policy_state["backoff_until"]


async def test_lifecycle_summary_reports_deferred_and_excluded_states(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-summary-1",
            connection_target="policy-summary-1",
            name="Summary Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB not responsive",
            },
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Summary Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "ADB not responsive",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    policy = await build_lifecycle_policy(db_session, device)
    summary = build_lifecycle_policy_summary(policy)
    assert summary["state"] == "deferred_stop"
    assert summary["label"] == "Stopping Soon"

    with state_write_guard.bypass():
        device.lifecycle_policy_state = {
            **(device.lifecycle_policy_state or {}),
            "stop_pending": False,
            "stop_pending_reason": None,
        }
    await db_session.commit()

    policy = await build_lifecycle_policy(db_session, device)
    summary = build_lifecycle_policy_summary(policy)
    assert summary["state"] == "excluded"
    assert summary["detail"] == "Excluded from Summary Run"


def test_lifecycle_summary_surfaces_reconciler_start_failure() -> None:
    summary = build_lifecycle_policy_summary(
        {
            "recovery_state": "idle",
            "last_failure_source": "appium_reconciler",
            "last_failure_reason": "port_occupied",
            "backoff_until": None,
        }
    )

    assert summary["state"] == "recoverable"
    assert summary["label"] == "Start Failed"
    assert summary["detail"] == "port_occupied"


async def test_clear_pending_auto_stop_on_recovery_drops_intent_and_records_incident(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-clear-pending-1",
            connection_target="lifecycle-clear-pending-1",
            name="Clear Pending Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB not responsive",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "node_health",
                "last_failure_reason": "Probe failed",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.commit()

    cleared = await _make_svc().clear_pending_auto_stop_on_recovery(
        db_session,
        device,
        source="node_health",
        reason="Node health checks recovered",
    )
    await db_session.commit()
    assert cleared is True

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
    assert reloaded.lifecycle_policy_state["stop_pending_reason"] is None
    assert reloaded.lifecycle_policy_state["stop_pending_since"] is None

    incident_stmt = select(DeviceEvent).where(
        DeviceEvent.device_id == device.id,
        DeviceEvent.event_type == DeviceEventType.lifecycle_recovered,
    )
    incidents = list((await db_session.execute(incident_stmt)).scalars().all())
    assert len(incidents) == 1
    details = incidents[0].details or {}
    detail = details.get("detail", "")
    assert "ADB not responsive" in detail
    assert "deferred stop" in detail.lower()


async def test_clear_pending_auto_stop_on_recovery_no_op_when_not_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-clear-pending-2",
            connection_target="lifecycle-clear-pending-2",
            name="No Pending Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": False,
                "stop_pending_reason": None,
                "stop_pending_since": None,
                "last_action": "node_monitor_recovered",
            },
        )
    db_session.add(device)
    await db_session.commit()

    cleared = await _make_svc().clear_pending_auto_stop_on_recovery(
        db_session,
        device,
        source="node_health",
        reason="Node health checks recovered",
    )
    assert cleared is False

    incident_stmt = select(DeviceEvent).where(DeviceEvent.device_id == device.id)
    incidents = list((await db_session.execute(incident_stmt)).scalars().all())
    assert incidents == []


async def test_handle_session_finished_drops_intent_when_healthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-finish-healthy",
            connection_target="lifecycle-finish-healthy",
            name="Finish Healthy",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB not responsive",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "node_health",
                "last_failure_reason": "ADB not responsive",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4781,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4781,
            pid=0,
            active_connection_target="",
        )
    db_session.add(node)
    await db_session.commit()

    await device_health.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
        publisher=event_bus,
    )
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy", publisher=event_bus)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    # CLEARED_RECOVERED: intent dropped, no auto-stop. Callers must use the
    # explicit outcome (not "not AUTO_STOPPED") to decide whether to restore
    # availability — this is the contract that replaces the old True/False
    # boolean.
    assert stopped is DeferredStopOutcome.CLEARED_RECOVERED

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
    # last_action must be refreshed so the audit trail does not show a stale
    # ``auto_stop_deferred`` after the intent was cleared by the healthy
    # session-end branch (see ``clear_pending_auto_stop_on_recovery``).
    assert reloaded.lifecycle_policy_state["last_action"] == "auto_stop_cleared"
    # handle_session_finished itself does not touch operational_state —
    # restoration is the caller's responsibility (covered by the integration
    # test test_session_sync_restores_busy_after_healthy_drop).
    assert reloaded.operational_state == DeviceOperationalState.busy


async def test_handle_session_finished_executes_stop_when_unhealthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-finish-unhealthy",
            connection_target="lifecycle-finish-unhealthy",
            name="Finish Unhealthy",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB not responsive",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "node_health",
                "last_failure_reason": "ADB not responsive",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.flush()
    await db_session.commit()

    await device_health.apply_node_state_transition(
        db_session,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
        publisher=event_bus,
    )
    await device_health.update_device_checks(
        db_session, device, healthy=False, summary="Probe failed", publisher=event_bus
    )
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert stopped is DeferredStopOutcome.AUTO_STOPPED

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
    assert reloaded.operational_state == DeviceOperationalState.offline  # complete_auto_stop ran


async def test_handle_session_finished_executes_stop_when_node_not_running(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-finish-node-stopped",
            connection_target="lifecycle-finish-node-stopped",
            name="Finish Node Stopped",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "Disconnected",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "device_checks",
                "last_failure_reason": "Disconnected",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.flush()
    # Node already stopped - even if health checks read healthy, complete_auto_stop must still run.
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4783,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.stopped,
            desired_port=None,
            pid=None,
            active_connection_target=None,
        )
    db_session.add(node)
    await db_session.commit()
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy", publisher=Mock())
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert stopped is DeferredStopOutcome.AUTO_STOPPED

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
    assert reloaded.operational_state == DeviceOperationalState.offline


async def test_handle_session_finished_returns_no_pending_when_intent_absent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-no-pending",
            connection_target="lifecycle-no-pending",
            name="No Pending",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={"stop_pending": False, "last_action": "idle"},
        )
    db_session.add(device)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING


async def test_handle_session_finished_clears_stale_session_running_suppression(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``attempt_auto_recovery`` records ``recovery_suppressed_reason="A client
    session is still running"`` when blocked by an active session but does NOT
    set ``stop_pending``. Without an explicit clear on session end, the
    suppression sticks forever and the dashboard renders the device as
    ``Unhealthy: A client session is still running`` long after the session
    finished. Regression test for that stale-state leak.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-session-suppression",
            connection_target="lifecycle-session-suppression",
            name="Session Suppression",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": False,
                "last_action": "recovery_suppressed",
                "last_failure_source": "node_health",
                "last_failure_reason": "probe timed out",
                "recovery_suppressed_reason": "A client session is still running",
            },
        )
    db_session.add(device)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state["recovery_suppressed_reason"] is None


async def test_handle_session_finished_applies_held_graceful_stop_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """End-to-end: a graceful-stop intent registered while a client session is
    running stays held by the intent reconciler, then applies on the same tick
    the session ends via ``handle_session_finished``.

    Exercises the lifecycle-policy → reconciler handshake added so that
    intent-driven deferrals (which do not touch ``lifecycle_policy_state``)
    still converge promptly when the held session finishes; without the
    up-front reconcile, the held intent would wait for the next full scan.
    """
    from app.devices.services.intent_reconciler import reconcile_device

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-held-intent",
            connection_target="lifecycle-held-intent",
            name="Held Intent",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={"stop_pending": False, "last_action": "idle"},
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4796,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4796,
            pid=42,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    session = Session(
        session_id="held-intent-session",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="held intent integration",
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": PRIORITY_HEALTH_FAILURE},
            ),
        ],
    )
    await db_session.commit()

    # Held while the session is running.
    await reconcile_device(db_session, device.id)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False

    # Session ends, then the handshake applies the held intent.
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()
    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_handle_session_finished_returns_running_session_exists_under_lock(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Authoritative running-session check happens under the device row lock.

    Even when a caller pre-validated outside the lock, a session inserted
    between that pre-check and the locked check must be respected: the helper
    must return RUNNING_SESSION_EXISTS instead of auto-stopping.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-toctou",
            connection_target="lifecycle-toctou",
            name="TOCTOU Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB not responsive",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "device_checks",
                "last_failure_reason": "ADB not responsive",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.flush()
    new_session = Session(
        session_id="sess-toctou-fresh",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(new_session)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.RUNNING_SESSION_EXISTS

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state is not None
    # State must be untouched because we bailed before doing any work.
    assert reloaded.lifecycle_policy_state["stop_pending"] is True
    assert reloaded.lifecycle_policy_state["last_action"] == "auto_stop_deferred"
    # Device must still be busy — caller (session_sync) leaves the new session in charge.
    assert reloaded.operational_state == DeviceOperationalState.busy


async def test_handle_session_finished_clears_intent_on_healthy_projection(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When derived health is healthy but ``last_failure_*`` still describes
    a recent failure, the row-derived projection is canonical.

    If the projection is wrong, the next failed probe will re-enter
    ``handle_health_failure`` and re-arm the deferred stop.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="lifecycle-stale-healthy",
            connection_target="lifecycle-stale-healthy",
            name="Stale Healthy",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            lifecycle_policy_state={
                "stop_pending": True,
                "stop_pending_reason": "ADB hung",
                "stop_pending_since": "2026-05-04T10:00:00+00:00",
                "last_action": "auto_stop_deferred",
                "last_failure_source": "node_health",
                "last_failure_reason": "ADB hung",
                "recovery_suppressed_reason": None,
            },
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4795,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4795,
            pid=0,
            active_connection_target="",
        )
    db_session.add(node)
    await db_session.commit()

    # Health reads healthy even though last_failure_* still describes a
    # current failure. The decision is to trust the derived health projection.
    await device_health.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
        publisher=event_bus,
    )
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy", publisher=event_bus)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert outcome is DeferredStopOutcome.CLEARED_RECOVERED

    await db_session.refresh(reloaded)
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
    assert reloaded.lifecycle_policy_state["last_action"] == "auto_stop_cleared"
    # last_failure_* is preserved (historical) but no longer drives behavior.
    assert reloaded.lifecycle_policy_state["last_failure_reason"] == "ADB hung"


def test_lifecycle_run_import_order_is_acyclic() -> None:
    import importlib

    lifecycle_policy_summary_mod = importlib.import_module("app.devices.services.lifecycle_policy_summary")
    run_service = importlib.import_module("app.runs.service")

    assert hasattr(lifecycle_policy_summary_mod, "build_lifecycle_policy")
    assert hasattr(run_service, "reservation_entry_is_excluded")


async def test_lifecycle_policy_suppression_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    db = AsyncMock()
    device = SimpleNamespace(
        id=uuid.uuid4(),
        hold=DeviceHold.maintenance,
        lifecycle_policy_state={},
        recovery_allowed=True,
        review_required=False,
        review_reason=None,
        recovery_blocked_reason=None,
        operational_state=DeviceOperationalState.offline,
        appium_node=None,
    )
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        lifecycle_policy_module, "write_state", lambda target, state: setattr(target, "lifecycle_policy_state", state)
    )
    suppressed = AsyncMock(return_value="suppressed")
    monkeypatch.setattr(LifecyclePolicyActionsService, "record_recovery_suppressed", suppressed)

    svc = _make_svc(publisher=event_bus)
    assert await svc.handle_health_failure(db, device, source="checks", reason="bad") == "suppressed"

    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(lifecycle_policy_module, "loaded_node", lambda _device: None)
    monkeypatch.setattr(lifecycle_policy_module, "is_ready_for_use_async", AsyncMock(return_value=True))
    mock_has_running = AsyncMock(return_value=False)
    monkeypatch.setattr(LifecyclePolicyActionsService, "has_running_client_session", mock_has_running)

    with state_write_guard.bypass():
        device.hold = None
    device.recovery_allowed = False
    assert await svc.attempt_auto_recovery(db, device, source="checks", reason="reconnected") == "suppressed"

    device.recovery_allowed = True
    with state_write_guard.bypass():
        device.hold = DeviceHold.maintenance
    assert await svc.attempt_auto_recovery(db, device, source="checks", reason="reconnected") == "suppressed"

    with state_write_guard.bypass():
        device.hold = None
    mock_has_running.return_value = True
    assert await svc.attempt_auto_recovery(db, device, source="checks", reason="reconnected") == "suppressed"

    mock_has_running.return_value = False
    with state_write_guard.bypass():
        device.lifecycle_policy_state = {"backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    assert await svc.attempt_auto_recovery(db, device, source="checks", reason="reconnected") is False
    db.commit.assert_awaited()


async def test_attempt_auto_recovery_rejoin_and_busy_autostop_success_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    db.add = lambda _row: None
    run = SimpleNamespace(id=uuid.uuid4(), name="active-run", state=RunState.active)
    excluded_entry = SimpleNamespace(excluded=True, excluded_until=None, exclusion_reason="flaky")
    node = SimpleNamespace(observed_running=True)
    device = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        hold=None,
        lifecycle_policy_state={},
        recovery_allowed=True,
        review_required=False,
        review_reason=None,
        recovery_blocked_reason=None,
        operational_state=DeviceOperationalState.offline,
        appium_node=node,
    )
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=device))
    monkeypatch.setattr(lifecycle_policy_module, "loaded_node", lambda _device: node)
    monkeypatch.setattr(LifecyclePolicyActionsService, "has_running_client_session", AsyncMock(return_value=False))
    monkeypatch.setattr(lifecycle_policy_module, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, excluded_entry)),
    )
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "reservation_entry_is_excluded",
        lambda entry: bool(entry and entry.excluded),
    )
    monkeypatch.setattr(lifecycle_policy_module.device_locking, "lock_device", AsyncMock(return_value=device))
    mock_restore_run = AsyncMock(return_value=(run, excluded_entry))
    monkeypatch.setattr(LifecyclePolicyActionsService, "restore_run_if_needed", mock_restore_run)
    monkeypatch.setattr(lifecycle_policy_module, "record_event", AsyncMock())
    mock_set_hold = AsyncMock()
    monkeypatch.setattr(lifecycle_policy_module, "set_hold", mock_set_hold)
    monkeypatch.setattr(
        lifecycle_policy_module, "write_state", lambda target, state: setattr(target, "lifecycle_policy_state", state)
    )
    monkeypatch.setattr(
        lifecycle_policy_module.lifecycle_incident_service,
        "record_lifecycle_incident",
        AsyncMock(),
    )

    viability = AsyncMock()
    viability.run_session_viability_probe = AsyncMock(return_value={"status": "passed"})
    svc = _make_svc(publisher=event_bus, viability=viability)
    assert await svc.attempt_auto_recovery(db, device, source="checks", reason="reconnected") is True
    mock_restore_run.assert_awaited_once()
    mock_set_hold.assert_awaited_with(
        device,
        DeviceHold.reserved,
        reason="Rejoined run after checks: reconnected",
        severity="info",
        publisher=event_bus,
    )

    busy = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        hold=None,
        lifecycle_policy_state={},
        recovery_allowed=True,
        review_required=False,
        review_reason=None,
        recovery_blocked_reason=None,
        operational_state=DeviceOperationalState.busy,
        appium_node=node,
    )
    lifecycle_policy_module._reload_device.return_value = busy
    lifecycle_policy_module.device_locking.lock_device.return_value = busy
    lifecycle_policy_module.run_reservation_service.get_device_reservation_with_entry.return_value = (None, None)
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "reservation_entry_is_excluded",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        lifecycle_policy_module,
        "ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.offline),
    )
    machine = SimpleNamespace(transition=AsyncMock())
    monkeypatch.setattr(lifecycle_policy_module, "_MACHINE", machine)

    assert await svc.attempt_auto_recovery(db, busy, source="checks", reason="reconnected") is True
    machine.transition.assert_awaited()
    assert machine.transition.await_args.args[1] is lifecycle_policy_module.TransitionEvent.AUTO_STOP_EXECUTED


async def test_attempt_auto_recovery_records_backoff_when_restart_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = SimpleNamespace(
        id=uuid.uuid4(),
        recovery_allowed=True,
        review_required=False,
        review_reason=None,
        recovery_blocked_reason=None,
        lifecycle_policy_state={},
        operational_state=DeviceOperationalState.offline,
        hold=None,
        host_id=None,
        appium_node=None,
    )
    run = SimpleNamespace(id=uuid.uuid4(), name="recovery-run")
    db = AsyncMock()
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=device))
    monkeypatch.setattr(lifecycle_policy_module, "policy_state", lambda _device: {})
    monkeypatch.setattr(lifecycle_policy_module, "write_state", lambda _device, state: setattr(_device, "state", state))
    monkeypatch.setattr(LifecyclePolicyActionsService, "has_running_client_session", AsyncMock(return_value=False))
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(run, None)),
    )
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service, "reservation_entry_is_excluded", lambda _: False
    )
    monkeypatch.setattr(lifecycle_policy_module, "loaded_node", lambda _device: None)
    monkeypatch.setattr(lifecycle_policy_module, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lifecycle_policy_module.lifecycle_incident_service,
        "record_lifecycle_incident",
        AsyncMock(),
    )

    assert (
        await _make_svc(publisher=event_bus).attempt_auto_recovery(
            db,
            device,  # type: ignore[arg-type]
            source="device_checks",
            reason="reconnected",
        )
        is False
    )

    assert device.state["last_action"] == "recovery_failed"
    assert device.state["recovery_suppressed_reason"] == "Automatic restart failed"
    assert lifecycle_policy_module.lifecycle_incident_service.record_lifecycle_incident.await_count == 2
    db.commit.assert_awaited()


async def test_attempt_auto_recovery_start_and_probe_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.commits = 0

        def add(self, item: object) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def refresh(self, _item: object) -> None:
            return None

    device = SimpleNamespace(
        id=uuid.uuid4(),
        recovery_allowed=True,
        review_required=False,
        review_reason=None,
        recovery_blocked_reason=None,
        lifecycle_policy_state={},
        operational_state=DeviceOperationalState.offline,
        hold=None,
        host_id=uuid.uuid4(),
        appium_node=None,
    )
    db = FakeDb()
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        lifecycle_policy_module,
        "write_state",
        lambda target, state: setattr(target, "lifecycle_policy_state", dict(state)),
    )
    monkeypatch.setattr(lifecycle_policy_module.device_locking, "lock_device", AsyncMock(return_value=device))
    monkeypatch.setattr(LifecyclePolicyActionsService, "has_running_client_session", AsyncMock(return_value=False))
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        lifecycle_policy_module.run_reservation_service,
        "reservation_entry_is_excluded",
        lambda _entry: False,
    )
    monkeypatch.setattr(lifecycle_policy_module, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(lifecycle_policy_module, "candidate_ports", AsyncMock(return_value=[4723]))
    monkeypatch.setattr(lifecycle_policy_module, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(lifecycle_policy_module, "register_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(lifecycle_policy_module, "record_event", AsyncMock())
    monkeypatch.setattr(
        lifecycle_policy_module,
        "ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.available),
    )
    monkeypatch.setattr(lifecycle_policy_module._MACHINE, "transition", AsyncMock())
    monkeypatch.setattr(lifecycle_policy_module.lifecycle_incident_service, "record_lifecycle_incident", AsyncMock())
    probe_order: list[str] = []

    async def probe_after_wait(*_args: object, **_kwargs: object) -> dict[str, str]:
        probe_order.append("probe")
        return {"status": "passed"}

    viability1 = AsyncMock()
    viability1.run_session_viability_probe = AsyncMock(side_effect=probe_after_wait)

    async def observe_node_running(*_args: object, **_kwargs: object) -> object:
        probe_order.append("wait")
        return SimpleNamespace(observed_running=True)

    mock_node_manager = AsyncMock()
    mock_node_manager.wait_for_node_running = AsyncMock(side_effect=observe_node_running)

    settings_with_grid = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 5,
            "general.lifecycle_recovery_backoff_max_sec": 20,
            "general.lifecycle_recovery_review_threshold": 5,
            "grid.hub_url": "http://grid:4444",
        }
    )
    svc = _make_svc(
        publisher=event_bus, settings=settings_with_grid, viability=viability1, node_manager=mock_node_manager
    )
    assert (
        await svc.attempt_auto_recovery(
            db,
            device,
            source="device_checks",
            reason="reconnected",
        )
        is True
    )  # type: ignore[arg-type]
    assert db.added
    lifecycle_policy_module.register_intents_and_reconcile.assert_awaited()
    lifecycle_policy_module._MACHINE.transition.assert_awaited()
    # wait_for_node_running must fire before run_session_viability_probe; probing
    # before agent start-up yields false negatives.
    assert probe_order == ["wait", "probe"]

    failing = SimpleNamespace(**device.__dict__)
    failing.id = uuid.uuid4()
    with state_write_guard.bypass():
        failing.lifecycle_policy_state = {}
    failing.appium_node = SimpleNamespace(observed_running=True)
    db2 = FakeDb()
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=failing))
    monkeypatch.setattr(lifecycle_policy_module.device_locking, "lock_device", AsyncMock(return_value=failing))
    mock_complete_auto_stop = AsyncMock()
    monkeypatch.setattr(LifecyclePolicyActionsService, "complete_auto_stop", mock_complete_auto_stop)
    monkeypatch.setattr(
        lifecycle_policy_module,
        "_set_backoff",
        lambda state, *, settings: "2026-05-13T12:00:00+00:00",
    )

    viability2 = AsyncMock()
    viability2.run_session_viability_probe = AsyncMock(return_value={"status": "failed", "error": "probe failed"})
    svc2 = _make_svc(publisher=event_bus, viability=viability2)
    assert (
        await svc2.attempt_auto_recovery(
            db2,
            failing,
            source="device_checks",
            reason="still bad",
        )
        is False
    )  # type: ignore[arg-type]
    assert failing.lifecycle_policy_state["recovery_suppressed_reason"] == "Recovery probe failed"
    mock_complete_auto_stop.assert_awaited_once()
