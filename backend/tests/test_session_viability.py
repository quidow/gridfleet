import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.capability import DeviceCapabilityService
from app.hosts.models import Host
from app.sessions import service_viability as session_viability
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY
from app.sessions.service_viability import (
    _PROBE_ALWAYS_MATCH_KEYS,
    SessionViabilityService,
    _filter_probe_always_match,
    _parse_timestamp,
    _should_run_scheduled_probe,
    get_session_viability,
    grid_probe_response_to_result,
)
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
from tests.helpers import (
    create_reservation,
    get_session_viability_control_plane_state,
    set_session_viability_control_plane_entry,
)
from tests.helpers import test_event_bus as _test_event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

# Module-level service instance used by local wrappers.
# Tests that need to intercept method calls should patch via
# ``monkeypatch.setattr(_svc, 'probe_session_direct', ...)`` or
# ``patch.object(SessionViabilityService, 'method', ...)``.
_svc = SessionViabilityService(
    publisher=_test_event_bus,
    settings=FakeSettingsReader({}),
    session_factory=AsyncMock(),
    capability=DeviceCapabilityService(),
    health=AsyncMock(),
)


@pytest.fixture(autouse=True)
def _isolate_module_svc() -> Iterator[None]:
    """Restore the shared module-level ``_svc`` state after every test.

    Several tests do ``monkeypatch.setattr(_svc, "probe_session_direct", ...)``.
    Because that method lives on the class, monkeypatch's undo restores it as an
    *instance* attribute on ``_svc`` — a residual that shadows a later test's
    ``patch.object(SessionViabilityService, ...)``, so the real probe runs and a
    passing-probe test flaps the device offline. Snapshotting and restoring
    ``__dict__`` gives each test a clean shared instance. This fixture is autouse
    with no dependencies, so it tears down *after* ``monkeypatch`` undoes its
    changes — clearing whatever residue monkeypatch leaves behind.
    """
    baseline = dict(_svc.__dict__)
    yield
    _svc.__dict__.clear()
    _svc.__dict__.update(baseline)


async def run_session_viability_probe(
    db: AsyncSession,
    device: Device,
    *,
    checked_by: object,
    settings: FakeSettingsReader | None = None,
) -> dict[str, Any]:
    _svc._settings = settings or FakeSettingsReader({})
    return await _svc.run_session_viability_probe(db, device, checked_by=checked_by)


async def probe_session_direct(
    capabilities: dict[str, Any],
    timeout_sec: int,
    *,
    target: str | None = None,
    settings: FakeSettingsReader | None = None,
) -> tuple[bool, str | None]:
    svc = SessionViabilityService(
        publisher=Mock(),
        settings=settings or FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    return await svc.probe_session_direct(capabilities, timeout_sec, target=target)


async def _check_due_devices(db: AsyncSession, *, settings: FakeSettingsReader | None = None) -> None:
    _svc._settings = settings or FakeSettingsReader({})
    await _svc.check_due_devices(db)


async def test_session_viability_state_is_not_persisted_in_device_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-config-001",
            connection_target="probe-config-001",
            name="Config Cleanup Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_config={"session_viability": {"status": "failed"}},
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4729,
            grid_url="http://node-grid:4444/wd/hub",
            desired_state=AppiumDesiredState.stopped,
            desired_port=None,
            pid=None,
            active_connection_target=None,
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "failed"
    await db_session.refresh(loaded_device)
    assert "session_viability" not in (loaded_device.device_config or {})


async def test_run_session_viability_probe_records_success(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-001",
            connection_target="probe-001",
            name="Probe Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ) as probe_mock,
    ):
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "passed"
    assert result["error"] is None
    assert result["checked_by"] == "manual"
    await db_session.refresh(loaded_device)
    persisted = await get_session_viability(db_session, loaded_device)
    assert persisted is not None
    assert persisted["status"] == "passed"
    assert persisted["last_succeeded_at"] == persisted["last_attempted_at"]
    assert loaded_device.operational_state == DeviceOperationalState.available
    probe_mock.assert_awaited_once()
    probe_capabilities = probe_mock.await_args.args[0]
    assert probe_capabilities["platformName"] == "Android"
    assert probe_capabilities["gridfleet:probeSession"] is True
    assert probe_capabilities["gridfleet:testName"] == session_viability.PROBE_TEST_NAME


async def test_recovery_session_viability_probe_allows_offline_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-recovery-001",
            connection_target="probe-recovery-001",
            name="Recovery Probe Device",
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
        node = AppiumNode(
            device_id=device.id,
            port=4733,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4733,
            pid=0,
            active_connection_target="",
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="recovery")

    assert result["status"] == "passed"
    await db_session.refresh(loaded_device)
    assert loaded_device.operational_state == DeviceOperationalState.available


async def test_run_session_viability_probe_uses_running_avd_active_target(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="manager_generated",
            identity_scope="host",
            identity_value="avd:Pixel_6_API_35",
            connection_target="Pixel_6_API_35",
            name="Pixel 6 AVD",
            os_version="15",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.usb,
            verified_at=datetime.now(UTC),
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://node-grid:4444/wd/hub",
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node
    loaded_device.host = db_host

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(True, None),
    ) as probe_mock:
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "passed"
    assert probe_mock.await_args is not None
    capabilities = probe_mock.await_args.args[0]
    assert capabilities["appium:udid"] == "emulator-5554"
    assert capabilities["appium:gridfleet:deviceId"] == str(device.id)
    assert capabilities["gridfleet:probeSession"] is True
    assert probe_mock.await_args.kwargs["target"] == f"http://{db_host.ip}:{loaded_node.port}"


async def test_run_session_viability_probe_writes_probe_row_on_ack(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-row-ack",
            connection_target="probe-row-ack",
            name="Probe Row Ack",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            verified_at=datetime.now(UTC),
        )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=None,
            port=4723,
            grid_url="http://node-grid:4444/wd/hub",
            active_connection_target="probe-row-ack",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1234,
        )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(True, None),
    ):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        )

    rows = (
        (
            await db_session.execute(
                select(Session).where(Session.device_id == device.id, Session.test_name == PROBE_TEST_NAME)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status is SessionStatus.passed
    assert row.session_id.startswith("probe-")
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "scheduled"


async def test_run_session_viability_probe_writes_probe_row_on_refusal(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-row-refuse",
            connection_target="probe-row-refuse",
            name="Probe Row Refuse",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            verified_at=datetime.now(UTC),
        )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=None,
            port=4723,
            grid_url="http://node-grid:4444/wd/hub",
            active_connection_target="probe-row-refuse",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1234,
        )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(False, "Session probe failed"),
    ):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
        )

    row = (
        await db_session.execute(
            select(Session).where(Session.device_id == device.id, Session.test_name == PROBE_TEST_NAME)
        )
    ).scalar_one()
    assert row.status is SessionStatus.failed
    assert row.error_type == "probe_refused"
    assert row.error_message == "Session probe failed"
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "manual"


async def test_run_session_viability_probe_rejects_non_available_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-002",
            connection_target="probe-002",
            name="Busy Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    try:
        await run_session_viability_probe(db_session, device, checked_by="manual")
    except ValueError as exc:
        assert "available devices" in str(exc)
    else:
        raise AssertionError("Expected run_session_viability_probe to reject busy devices")


async def test_check_due_devices_respects_interval(db_session: AsyncSession, db_host: Host) -> None:
    settings_service._cache["general.session_viability_interval_sec"] = 86400

    with state_write_guard.bypass():
        due = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-003",
            connection_target="probe-003",
            name="Due Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    with state_write_guard.bypass():
        recent = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-004",
            connection_target="probe-004",
            name="Recent Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add_all([due, recent])
    await db_session.commit()
    await set_session_viability_control_plane_entry(
        db_session,
        str(recent.id),
        {
            "status": "passed",
            "last_attempted_at": "2099-01-01T00:00:00+00:00",
            "last_succeeded_at": "2099-01-01T00:00:00+00:00",
            "error": None,
            "checked_by": "scheduled",
        },
    )

    with patch.object(SessionViabilityService, "run_session_viability_probe", new_callable=AsyncMock) as mock_probe:
        await _check_due_devices(db_session)

    assert mock_probe.await_count == 1
    assert mock_probe.await_args is not None
    assert mock_probe.await_args.kwargs["checked_by"] == "scheduled"
    assert mock_probe.await_args.args[1].connection_target == "probe-003"
    control_plane_state = await get_session_viability_control_plane_state(db_session)
    assert str(recent.id) in control_plane_state["state"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_due_devices_excludes_reserved_device(db_session: AsyncSession, db_host: Host) -> None:
    """A device with hold=NULL but an active reservation must not be probed."""
    settings_service._cache["general.session_viability_interval_sec"] = 86400

    with state_write_guard.bypass():
        reserved = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-reserved",
            connection_target="probe-reserved",
            name="Reserved Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(reserved)
    await db_session.commit()
    await create_reservation(db_session, device_id=reserved.id)
    await db_session.commit()

    with patch.object(SessionViabilityService, "run_session_viability_probe", new_callable=AsyncMock) as mock_probe:
        await _check_due_devices(db_session)

    assert mock_probe.await_count == 0


async def test_probe_session_direct_passes_through_transport_error_as_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_viability.appium_direct,
        "create_session",
        AsyncMock(return_value=(None, "ConnectError while calling http://node:4723/session", True)),
    )

    ok, error = await probe_session_direct({"platformName": "iOS"}, timeout_sec=5, target="http://node:4723")

    assert ok is False
    assert error == "Session create request failed: ConnectError while calling http://node:4723/session"


async def test_probe_session_direct_none_target_is_indeterminate() -> None:
    ok, error = await probe_session_direct({"platformName": "iOS"}, timeout_sec=5, target=None)

    assert ok is False
    assert error is not None and error.startswith("Session create request failed:")


async def test_probe_session_direct_creates_and_terminates_against_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_mock = AsyncMock(return_value=("session-1", None, False))
    terminate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(session_viability.appium_direct, "create_session", create_mock)
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", terminate_mock)

    ok, error = await probe_session_direct(
        {"platformName": "iOS"},
        timeout_sec=5,
        target="http://node:4723/",
    )

    assert ok is True
    assert error is None
    create_mock.assert_awaited_once_with(
        "http://node:4723",
        {"capabilities": {"alwaysMatch": {"platformName": "iOS"}, "firstMatch": [{}]}},
        timeout=5,
    )
    terminate_mock.assert_awaited_once_with("http://node:4723", "session-1", timeout=5)


def test_grid_probe_response_to_result_maps_all_shapes() -> None:
    assert grid_probe_response_to_result((True, None)).status == "ack"
    assert grid_probe_response_to_result((False, None)).status == "refused"

    refused = grid_probe_response_to_result((False, "device offline"))
    assert refused.status == "refused"
    assert refused.detail == "device offline"

    indeterminate = grid_probe_response_to_result((False, "Session create request failed: ConnectError"))
    assert indeterminate.status == "indeterminate"
    assert indeterminate.detail == "Session create request failed: ConnectError"


def test_session_viability_small_helpers_cover_error_shapes() -> None:
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None
    assert _parse_timestamp("2026-01-02T03:04:05Z") is not None


async def test_record_session_viability_result_preserves_previous_success_and_clears_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-record-001",
            connection_target="probe-record-001",
            name="Probe Record Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_config={"session_viability": {"status": "legacy"}},
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    passed = await _svc.record_session_viability_result(db_session, device, status="passed", checked_by="manual")
    failed = await _svc.record_session_viability_result(
        db_session,
        device,
        status="failed",
        error="probe failed",
        checked_by="scheduled",
    )

    assert passed["last_succeeded_at"] is not None
    assert failed["status"] == "failed"
    assert failed["last_succeeded_at"] == passed["last_succeeded_at"]
    assert "session_viability" not in (device.device_config or {})


async def test_should_run_scheduled_probe_covers_skip_and_due_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-schedule-001",
            connection_target="probe-schedule-001",
            name="Probe Schedule Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    assert await _should_run_scheduled_probe(db_session, device, 0) is False
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.busy
    assert await _should_run_scheduled_probe(db_session, device, 60) is False
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.available

    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    assert await _should_run_scheduled_probe(db_session, device, 60) is False

    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=True))
    await set_session_viability_control_plane_entry(
        db_session,
        str(device.id),
        {
            "status": "passed",
            "last_attempted_at": "not-a-date",
            "last_succeeded_at": None,
            "error": None,
            "checked_by": "scheduled",
        },
    )
    assert await _should_run_scheduled_probe(db_session, device, 60) is True

    await session_viability.control_plane_state_store.set_value(
        db_session,
        session_viability.SESSION_VIABILITY_RUNNING_NAMESPACE,
        str(device.id),
        {"started_at": "now"},
    )
    assert await _should_run_scheduled_probe(db_session, device, 60) is False


@pytest.mark.parametrize(
    ("create_return", "expected_error"),
    [
        # HTTP refusal: the node answered, session refused — surface the raw message.
        ((None, "bad caps", False), "bad caps"),
        # Non-JSON refusal body falls back to raw text in appium_direct.
        ((None, "plain body", False), "plain body"),
        # Empty error string still produces a deterministic refusal message.
        ((None, "", False), "Session create failed"),
    ],
)
async def test_probe_session_direct_create_failure_paths(
    create_return: tuple[None, str, bool], expected_error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_viability.appium_direct, "create_session", AsyncMock(return_value=create_return))

    ok, error = await probe_session_direct({"platformName": "Android"}, timeout_sec=3, target="http://node:4723")

    assert ok is False
    assert error == expected_error


async def test_probe_session_direct_cleanup_failure_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_viability.appium_direct, "create_session", AsyncMock(return_value=("session-1", None, False))
    )
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", AsyncMock(return_value=False))

    ok, error = await probe_session_direct({"platformName": "Android"}, timeout_sec=3, target="http://node:4723")

    assert ok is False
    assert error == "Session created but cleanup failed"


async def test_run_session_viability_probe_rejects_missing_running_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-no-node",
            connection_target="probe-no-node",
            name="No Node Probe Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_device.appium_node = None

    result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "failed"
    assert result["error"] == "Appium node is not running"


async def test_run_session_viability_probe_rejects_duplicate_and_not_ready(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-guard-001",
            connection_target="probe-guard-001",
            name="Probe Guard Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    await session_viability.control_plane_state_store.set_value(
        db_session,
        session_viability.SESSION_VIABILITY_RUNNING_NAMESPACE,
        str(device.id),
        {"started_at": "already"},
    )
    with pytest.raises(ValueError, match="already in progress"):
        await run_session_viability_probe(db_session, device, checked_by="manual")

    await session_viability.control_plane_state_store.delete_value(
        db_session,
        session_viability.SESSION_VIABILITY_RUNNING_NAMESPACE,
        str(device.id),
    )
    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.sessions.service_viability.readiness_error_detail_async",
        AsyncMock(return_value="not ready enough"),
    )

    with pytest.raises(ValueError, match="not ready enough"):
        await run_session_viability_probe(db_session, device, checked_by="manual")


async def test_run_session_viability_probe_changed_state_and_health_handler_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-handler-001",
            connection_target="probe-handler-001",
            name="Probe Handler Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_config={"session_viability": {"status": "failed"}},
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4780,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4780,
            pid=1234,
            active_connection_target="probe-handler-001",
        )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    # After Task 10: no SESSION_STARTED/SESSION_ENDED transitions; mark_dirty_and_reconcile
    # calls reconcile_device which calls lock_device again. Provide extra mocks.
    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.available, hold=None)
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(return_value=locked))
    # Patch reconcile_device to avoid real DB ops with MagicMock objects.
    monkeypatch.setattr(
        session_viability,
        "IntentService",
        MagicMock(
            return_value=MagicMock(
                mark_dirty_and_reconcile=AsyncMock(),
                mark_dirty=AsyncMock(),
            )
        ),
    )
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(
        session_viability,
        "_write_session_viability",
        AsyncMock(return_value={"status": "failed", "consecutive_failures": 1}),
    )
    handler = AsyncMock()
    _svc.configure_health_failure_handler(handler)
    try:
        state = await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
            settings=FakeSettingsReader(
                {
                    "general.session_viability_failure_threshold": 1,
                    "general.session_viability_timeout_sec": 5,
                }
            ),
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state == {"status": "failed", "consecutive_failures": 1}
    assert device.device_config == {}
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["reason"] == "Appium session viability probe failed"


async def test_run_session_viability_probe_restores_previous_state_on_exception(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-exception-001",
            connection_target="probe-exception-001",
            name="Probe Exception Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4781,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4781,
            pid=1234,
            active_connection_target="probe-exception-001",
        )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.offline, hold=None)
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(return_value=locked))
    # After Task 10: no _MACHINE; exception path calls mark_dirty_and_reconcile. Patch IntentService.
    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        session_viability,
        "IntentService",
        MagicMock(
            return_value=MagicMock(
                mark_dirty_and_reconcile=mark_dirty,
                mark_dirty=AsyncMock(),
            )
        ),
    )
    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.recovery,
            settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        )

    # Exception path calls mark_dirty_and_reconcile (not set_operational_state).
    mark_dirty.assert_awaited()


async def test_run_session_viability_probe_no_node_commit_and_available_exception_restore(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_node = MagicMock(
        id=uuid.uuid4(),
        operational_state=DeviceOperationalState.available,
        hold=None,
        appium_node=None,
        device_config={"session_viability": {"status": "legacy"}},
    )
    fake_db = AsyncMock()
    # device_is_reserved calls db.execute(...).first(); return None so the device reads as unreserved.
    fake_db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability, "_write_session_viability", AsyncMock(return_value={"status": "failed"}))

    state = await run_session_viability_probe(
        fake_db,
        no_node,
        checked_by=session_viability.SessionViabilityCheckedBy.manual,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
    )

    assert state["status"] == "failed"
    assert no_node.device_config == {}
    assert fake_db.commit.await_count >= 2

    device_id = uuid.uuid4()
    available = MagicMock(id=device_id, operational_state=DeviceOperationalState.available, hold=None)
    available.appium_node = MagicMock(observed_running=True)
    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.available, hold=None)
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(return_value=locked))
    # After Task 10: no _MACHINE; exception path calls mark_dirty_and_reconcile.
    mark_dirty2 = AsyncMock()
    monkeypatch.setattr(
        session_viability,
        "IntentService",
        MagicMock(
            return_value=MagicMock(
                mark_dirty_and_reconcile=mark_dirty2,
                mark_dirty=AsyncMock(),
            )
        ),
    )
    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            available,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
            settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        )

    # Exception path calls mark_dirty_and_reconcile (not set_operational_state).
    mark_dirty2.assert_awaited()


def test_classify_session_error_recognises_grid_no_slot() -> None:
    no_slot = "Could not start a new session. {value={error=session not created}} Driver info: driver.version: unknown"
    assert session_viability._classify_session_error(no_slot) == "grid_no_slot"
    assert session_viability._classify_session_error("ADB device 'X' not found within timeout") == "driver"
    assert session_viability._classify_session_error(None) is None


async def _run_failing_probe(
    db: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: str = "boom",
    threshold: int = 3,
    handler: AsyncMock | None = None,
) -> dict[str, object]:
    """Helper: drive ``run_session_viability_probe`` with a failing grid probe."""

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return threshold
        return 5

    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, error)))
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    if handler is not None:
        _svc.configure_health_failure_handler(handler)
    return await run_session_viability_probe(
        db,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        settings=FakeSettingsReader(
            {
                "general.session_viability_failure_threshold": _settings("general.session_viability_failure_threshold"),
                "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
            }
        ),
    )


def _make_viability_device(db_host: Host, suffix: str) -> tuple[Device, AppiumNode]:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"viab-strike-{suffix}",
            connection_target=f"viab-strike-{suffix}",
            name=f"Viability Strike {suffix}",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4799,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4799,
            pid=999,
            active_connection_target=f"viab-strike-{suffix}",
        )
    device.appium_node = node
    return device, node


async def test_single_viability_failure_does_not_escalate_below_threshold(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "below")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    try:
        state = await _run_failing_probe(
            db_session, device, monkeypatch, error="grid hiccup", threshold=3, handler=handler
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state["status"] == "failed"
    assert state.get("consecutive_failures") == 1
    handler.assert_not_awaited()


async def test_viability_escalates_after_threshold_consecutive_failures(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "threshold")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    try:
        for _ in range(3):
            # Each probe leaves the device offline; the recovery branch is what
            # lets the next iteration re-enter the probe path.
            with state_write_guard.bypass():
                device.operational_state = DeviceOperationalState.available
            await _run_failing_probe(db_session, device, monkeypatch, error="grid hiccup", threshold=3, handler=handler)
    finally:
        _svc.configure_health_failure_handler(None)

    final = await get_session_viability(db_session, device)
    assert final is not None and final["status"] == "failed"
    assert final["consecutive_failures"] == 3
    handler.assert_awaited_once()


async def test_passing_probe_resets_viability_failure_counter(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "reset")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return 3
        return 5

    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    _svc.configure_health_failure_handler(handler)
    try:
        # Two consecutive failures get to count=2.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, "transient")))
        for _ in range(2):
            with state_write_guard.bypass():
                device.operational_state = DeviceOperationalState.available
            await run_session_viability_probe(
                db_session,
                device,
                checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
                settings=FakeSettingsReader(
                    {
                        "general.session_viability_failure_threshold": _settings(
                            "general.session_viability_failure_threshold"
                        ),
                        "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
                    }
                ),
            )

        # A passing probe must reset the counter back to 0.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
        with state_write_guard.bypass():
            device.operational_state = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
        mid = await get_session_viability(db_session, device)
        assert mid is not None and mid["consecutive_failures"] == 0

        # One more failure must start the count over, not jump straight to threshold.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, "transient again")))
        with state_write_guard.bypass():
            device.operational_state = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
    finally:
        _svc.configure_health_failure_handler(None)

    final = await get_session_viability(db_session, device)
    assert final is not None and final["consecutive_failures"] == 1
    handler.assert_not_awaited()


async def test_write_session_viability_persists_error_category(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: ``error_category`` must survive the round-trip through
    the control-plane state store, not just live on the in-memory return value
    of ``_write_session_viability``. Without this, the ``_classify_session_error``
    classifier would silently stop being observable to operators."""
    device, node = _make_viability_device(db_host, "category")
    db_session.add_all([device, node])
    await db_session.commit()

    grid_error = (
        "Could not start a new session. {value={error=session not created}} Driver info: driver.version: unknown"
    )

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return 5  # below threshold so no escalation interferes
        return 5

    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, grid_error)))

    await run_session_viability_probe(
        db_session,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        settings=FakeSettingsReader(
            {
                "general.session_viability_failure_threshold": _settings("general.session_viability_failure_threshold"),
                "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
            }
        ),
    )

    persisted = await get_session_viability(db_session, device)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error_category"] == "grid_no_slot"

    # A passing probe must clear ``error_category`` so a recovered device does
    # not keep an old infra tag attached.
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.available
    await run_session_viability_probe(
        db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
    )
    after_pass = await get_session_viability(db_session, device)
    assert after_pass is not None
    assert after_pass["status"] == "passed"
    assert after_pass["error_category"] is None


async def test_run_session_viability_probe_passes_does_not_flap_offline_when_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Regression: a passing probe must not flap the device offline when a
    stale graceful-stop intent has marked ``node.stop_pending=True``.

    A stale ``connectivity:*`` stop intent can leave
    ``node.stop_pending=True`` on a fully-healthy device. The scheduled
    viability probe then runs, passes (Grid acks), and the post-probe
    restore path used the ``ready_operational_state(...)`` projection —
    which folded ``appium_node_stop_in_flight`` into the operational
    axis and returned ``offline``. The device transitioned busy →
    offline ("Session viability probe finished"), and seconds later the
    health-recovery loop flipped it back ("Health checks recovered"),
    producing a toast pair per probe cycle.

    The probe-passed branch is an event ("probe ok"), not a projection.
    It must drive SESSION_ENDED (busy → available) directly. Real offline
    transitions for in-flight stops belong to the connectivity loop and
    node_health, which fire on their own schedules with their own
    reasons.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-stop-pending-repro",
            connection_target="probe-stop-pending-repro",
            name="Probe Stop Pending Repro",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=12345,
            active_connection_target="probe-stop-pending-repro",
            stop_pending=True,
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    event_bus_capture.clear()
    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        await run_session_viability_probe(
            db_session,
            loaded_device,
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        )

    op_events = [
        payload
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed" and payload["device_id"] == str(loaded_device.id)
    ]
    spurious_offline = [p for p in op_events if p["new_operational_state"] == "offline"]
    assert spurious_offline == [], (
        "passing probe must not project transient stop_pending into operational_state; "
        f"got spurious offline event(s) {spurious_offline}"
    )
    await db_session.refresh(loaded_device)
    assert loaded_device.operational_state == DeviceOperationalState.available


def test_probe_always_match_routes_on_device_id_not_udid() -> None:
    """Probes must pin on the stable gridfleet:deviceId, never appium:udid.

    The slot stereotype no longer advertises appium:udid (it is a driver
    connection detail, not a routing key), so sending it in alwaysMatch would
    make Selenium's DefaultSlotMatcher reject the slot.
    """
    full_caps = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:udid": "emulator-5554",
        "appium:deviceName": "Pixel",
        "appium:gridfleet:deviceId": "abc-123",
        "gridfleet:probeSession": True,
        "gridfleet:testName": "gridfleet-probe",
    }

    filtered = _filter_probe_always_match(full_caps)

    assert "appium:udid" not in filtered
    assert "appium:deviceName" not in filtered
    assert filtered["appium:gridfleet:deviceId"] == "abc-123"
    assert filtered["platformName"] == "Android"
    assert filtered["gridfleet:probeSession"] is True
    assert "appium:udid" not in _PROBE_ALWAYS_MATCH_KEYS
    assert "appium:deviceName" not in _PROBE_ALWAYS_MATCH_KEYS
