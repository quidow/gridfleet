from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services import device_health
from app.services import lifecycle_policy as lifecycle_policy_module
from app.services.lifecycle_policy import (
    DeferredStopOutcome,
    attempt_auto_recovery,
    build_lifecycle_policy,
    build_lifecycle_policy_summary,
    clear_pending_auto_stop_on_recovery,
    handle_health_failure,
    handle_session_finished,
)

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _speed_up_recovery_probe_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0, raising=False)


async def _mark_device_available(_db: AsyncSession, device: Device, *, caller: str = "operator_route") -> None:
    device.operational_state = DeviceOperationalState.available


async def _event_types_for_device(db_session: AsyncSession, device_id: object) -> list[DeviceEventType]:
    result = await db_session.execute(
        select(DeviceEvent.event_type).where(DeviceEvent.device_id == device_id).order_by(DeviceEvent.created_at.asc())
    )
    return list(result.scalars().all())


async def test_idle_health_failure_stops_device(db_session: AsyncSession, db_host: Host) -> None:
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

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")

    await db_session.refresh(device)
    assert result == "stopped"
    assert device.operational_state == DeviceOperationalState.offline
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_failure_reason"] == "ADB not responsive"
    assert policy["last_action"] == "auto_stopped"


async def test_active_session_failure_defers_stop(db_session: AsyncSession, db_host: Host) -> None:
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

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")

    await db_session.refresh(device)
    assert result == "deferred"
    assert device.operational_state == DeviceOperationalState.busy
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["stop_pending"] is True
    assert policy["recovery_state"] == "waiting_for_session_end"
    assert await _event_types_for_device(db_session, device.id) == [DeviceEventType.lifecycle_deferred_stop]


async def test_reserved_idle_failure_excludes_run(db_session: AsyncSession, db_host: Host) -> None:
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

    await handle_health_failure(db_session, device, source="device_checks", reason="Health probe failed")

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

    await handle_health_failure(db_session, device, source="device_checks", reason="Health probe failed")
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    stopped = await handle_session_finished(db_session, device)

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["stop_pending"] is False
    assert policy["excluded_from_run"] is True


async def test_recovery_is_suppressed_when_auto_manage_disabled(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-recover-1",
        connection_target="policy-recover-1",
        name="Manual Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        auto_manage=False,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

    assert recovered is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] == "suppressed"
    assert policy["recovery_suppressed_reason"] == "Auto-manage is disabled"


async def test_recovery_is_suppressed_during_backoff(db_session: AsyncSession, db_host: Host) -> None:
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
    device.lifecycle_policy_state = {
        **(device.lifecycle_policy_state or {}),
        "backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    await db_session.commit()

    recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

    assert recovered is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] == "backoff"


async def test_successful_recovery_rejoins_run(db_session: AsyncSession, db_host: Host) -> None:
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

    manager = SimpleNamespace(start_node=AsyncMock(side_effect=_mark_device_available))
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ),
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

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
    assert DeviceEventType.lifecycle_run_restored in event_types
    assert DeviceEventType.lifecycle_recovered in event_types


async def test_recovery_rejoin_publishes_availability_event(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    async def fake_publish(name: str, payload: dict[str, object]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", fake_publish)

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

    manager = SimpleNamespace(start_node=AsyncMock(side_effect=_mark_device_available))
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ),
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

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
        current.operational_state = DeviceOperationalState.available
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

    manager = SimpleNamespace(start_node=AsyncMock())
    with patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

    assert recovered is False
    manager.start_node.assert_not_awaited()


async def test_failed_recovery_sets_backoff_and_keeps_exclusion(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    manager = SimpleNamespace(start_node=AsyncMock(side_effect=_mark_device_available))
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "failed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": None,
                "error": "Session create failed",
                "checked_by": "recovery",
            },
        ),
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

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

    manager = SimpleNamespace(start_node=AsyncMock(side_effect=_mark_device_available))
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
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
            ],
        ) as mock_probe,
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

    await db_session.refresh(device)
    assert recovered is True
    assert device.operational_state == DeviceOperationalState.available
    assert mock_probe.await_count == 2
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "auto_recovered"
    assert policy["backoff_until"] is None


async def test_deferred_stop_survives_restart_boundary(db_session: AsyncSession, db_host: Host) -> None:
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

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")
    assert result == "deferred"

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await handle_session_finished(db_session, reloaded)

    await db_session.refresh(reloaded)
    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert reloaded.operational_state == DeviceOperationalState.offline
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_failed_recovery_backoff_survives_restart_and_uses_settings(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    manager = SimpleNamespace(start_node=AsyncMock(side_effect=_mark_device_available))
    with (
        patch("app.services.lifecycle_policy.start_managed_node", new=manager.start_node),
        patch(
            "app.services.lifecycle_policy.settings_service.get",
            side_effect=lambda key: {
                "general.lifecycle_recovery_backoff_base_sec": 5,
                "general.lifecycle_recovery_backoff_max_sec": 20,
            }[key],
        ),
        patch(
            "app.services.session_viability.run_session_viability_probe",
            new_callable=AsyncMock,
            return_value={
                "status": "failed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": None,
                "error": "Probe failed",
                "checked_by": "recovery",
            },
        ),
    ):
        recovered = await attempt_auto_recovery(db_session, device, source="device_checks", reason="Healthy again")

    assert recovered is False
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    backoff_until = datetime.fromisoformat(device.lifecycle_policy_state["backoff_until"])
    assert 4 <= (backoff_until - datetime.now(UTC)).total_seconds() <= 6

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
    assert summary["label"] == "Deferred Stop"

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
    assert summary["label"] == "Node Start Failed"
    assert summary["detail"] == "port_occupied"


async def test_clear_pending_auto_stop_on_recovery_drops_intent_and_records_incident(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    cleared = await clear_pending_auto_stop_on_recovery(
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

    cleared = await clear_pending_auto_stop_on_recovery(
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
    )
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await handle_session_finished(db_session, reloaded)
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
    )
    await device_health.update_device_checks(db_session, device, healthy=False, summary="Probe failed")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await handle_session_finished(db_session, reloaded)
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
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await handle_session_finished(db_session, reloaded)
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
    outcome = await handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING


async def test_handle_session_finished_returns_running_session_exists_under_lock(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Authoritative running-session check happens under the device row lock.

    Even when a caller pre-validated outside the lock, a session inserted
    between that pre-check and the locked check must be respected: the helper
    must return RUNNING_SESSION_EXISTS instead of auto-stopping.
    """
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
    outcome = await handle_session_finished(db_session, reloaded)
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
    )
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await handle_session_finished(db_session, reloaded)
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

    lifecycle_policy = importlib.import_module("app.services.lifecycle_policy")
    lifecycle_policy_summary = importlib.import_module("app.services.lifecycle_policy_summary")
    run_service = importlib.import_module("app.services.run_service")

    assert lifecycle_policy.build_lifecycle_policy is lifecycle_policy_summary.build_lifecycle_policy
    assert hasattr(run_service, "reservation_entry_is_excluded")
