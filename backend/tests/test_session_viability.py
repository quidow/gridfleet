import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.services import session_viability
from app.services.session_viability import (
    _check_due_devices,
    _extract_session_error,
    _format_http_error,
    _get_grid_probe_client,
    _parse_timestamp,
    _should_run_scheduled_probe,
    get_session_viability,
    get_session_viability_control_plane_state,
    probe_session_via_agent_node,
    probe_session_via_grid,
    record_session_viability_result,
    run_session_viability_probe,
    set_session_viability_control_plane_entry,
)
from app.services.settings_service import settings_service

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_session_viability_state_is_not_persisted_in_device_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    node = AppiumNode(
        device_id=device.id,
        port=4729,
        grid_url="http://hub:4444",
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
            "app.services.session_viability.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.services.session_viability.appium_probe_session",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
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


async def test_recovery_session_viability_probe_allows_offline_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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
            "app.services.session_viability.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.services.session_viability.appium_probe_session",
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

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
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

    with patch(
        "app.services.session_viability.appium_probe_session",
        new_callable=AsyncMock,
        return_value=(True, None),
    ) as probe_mock:
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "passed"
    assert probe_mock.await_args is not None
    capabilities = probe_mock.await_args.kwargs["capabilities"]
    assert capabilities["appium:udid"] == "emulator-5554"
    assert capabilities["appium:gridfleet:deviceId"] == str(device.id)


async def test_run_session_viability_probe_rejects_non_available_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    with patch("app.services.session_viability.run_session_viability_probe", new_callable=AsyncMock) as mock_probe:
        await _check_due_devices(db_session)

    assert mock_probe.await_count == 1
    assert mock_probe.await_args is not None
    assert mock_probe.await_args.kwargs["checked_by"] == "scheduled"
    assert mock_probe.await_args.args[1].connection_target == "probe-003"
    control_plane_state = await get_session_viability_control_plane_state(db_session)
    assert str(recent.id) in control_plane_state["state"]


async def test_probe_session_via_grid_includes_exception_type_for_blank_http_error() -> None:
    request = httpx.Request("POST", "http://hub:4444/session")
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("", request=request))

    with patch("app.services.session_viability._get_grid_probe_client", return_value=mock_client):
        ok, error = await probe_session_via_grid({"platformName": "iOS"}, timeout_sec=5)

    assert ok is False
    assert error == "Session create request failed: ReadTimeout while calling http://hub:4444/session"


async def test_probe_session_via_grid_preserves_configured_base_path() -> None:
    create_response = MagicMock(spec=httpx.Response, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-1"}}
    delete_response = MagicMock(spec=httpx.Response, status_code=200)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=create_response)
    mock_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch("app.services.session_viability.settings_service.get", return_value="http://hub:4444/wd/hub"),
        patch("app.services.session_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "iOS"}, timeout_sec=5)

    assert ok is True
    assert error is None
    mock_client.post.assert_awaited_once_with(
        "http://hub:4444/wd/hub/session",
        json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}, "firstMatch": [{}]}},
        timeout=5,
    )
    mock_client.delete.assert_awaited_once_with("http://hub:4444/wd/hub/session/session-1", timeout=5)


def test_session_viability_small_helpers_cover_error_shapes() -> None:
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None
    assert _parse_timestamp("2026-01-02T03:04:05Z") is not None

    assert _extract_session_error({"value": {"message": "bad caps"}}) == "bad caps"
    assert _extract_session_error({"value": {"error": "invalid argument"}}) == "invalid argument"
    assert _extract_session_error({"message": "plain failure"}) == "plain failure"
    assert _extract_session_error([]) == "Session probe failed"

    response = httpx.Response(503, request=httpx.Request("GET", "http://hub/status"))
    assert _format_http_error(httpx.HTTPStatusError("", request=response.request, response=response)) == (
        "HTTPStatusError (HTTP 503)"
    )
    assert _format_http_error(httpx.ConnectError("", request=httpx.Request("GET", "http://hub/status"))) == (
        "ConnectError while calling http://hub/status"
    )


async def test_grid_probe_client_is_reused_and_close_resets_it() -> None:
    client = _get_grid_probe_client()
    assert _get_grid_probe_client() is client

    await session_viability.close()

    assert session_viability._grid_probe_client is None


async def test_record_session_viability_result_preserves_previous_success_and_clears_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    passed = await record_session_viability_result(db_session, device, status="passed", checked_by="manual")
    failed = await record_session_viability_result(
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
    device.operational_state = DeviceOperationalState.busy
    assert await _should_run_scheduled_probe(db_session, device, 60) is False
    device.operational_state = DeviceOperationalState.available

    monkeypatch.setattr("app.services.session_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    assert await _should_run_scheduled_probe(db_session, device, 60) is False

    monkeypatch.setattr("app.services.session_viability.is_ready_for_use_async", AsyncMock(return_value=True))
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
    ("create_response", "expected_error"),
    [
        (MagicMock(status_code=400, json=MagicMock(return_value={"value": {"message": "bad caps"}})), "bad caps"),
        (MagicMock(status_code=400, json=MagicMock(side_effect=ValueError), text="plain body"), "plain body"),
        (MagicMock(status_code=200, json=MagicMock(side_effect=ValueError)), "Session create returned invalid JSON"),
        (
            MagicMock(status_code=200, json=MagicMock(return_value={"value": {}})),
            "Session create did not return a session id",
        ),
    ],
)
async def test_probe_session_via_grid_create_failure_paths(create_response: MagicMock, expected_error: str) -> None:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=create_response)

    with (
        patch("app.services.session_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.services.session_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "Android"}, timeout_sec=3)

    assert ok is False
    assert error == expected_error


async def test_probe_session_via_grid_cleanup_failure_paths() -> None:
    create_response = MagicMock(status_code=200)
    create_response.json.return_value = {"sessionId": "session-1"}
    delete_response = MagicMock(status_code=500)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=create_response)
    mock_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch("app.services.session_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.services.session_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "Android"}, timeout_sec=3)

    assert ok is False
    assert error == "Session created but cleanup failed (500)"

    mock_client.delete = AsyncMock(side_effect=httpx.ConnectError("down"))
    with (
        patch("app.services.session_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.services.session_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "Android"}, timeout_sec=3)

    assert ok is False
    assert error == "Session created but cleanup failed: down"


async def test_probe_session_via_agent_node_rejects_missing_runtime_pieces(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-agent-001",
        connection_target="probe-agent-001",
        name="Probe Agent Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.appium_node = None

    ok, error = await probe_session_via_agent_node(db_session, device, {}, 5)
    assert (ok, error) == (False, "Appium node is not running")

    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid",
        pid=1,
        active_connection_target="probe-agent-001",
    )
    device.host_id = None
    ok, error = await probe_session_via_agent_node(db_session, device, {}, 5)
    assert (ok, error) == (False, "Device has no management host")

    device.host_id = uuid.uuid4()
    ok, error = await probe_session_via_agent_node(db_session, device, {}, 5)
    assert (ok, error) == (False, "Device management host was not found")


async def test_run_session_viability_probe_rejects_duplicate_and_not_ready(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr("app.services.session_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.services.session_viability.readiness_error_detail_async",
        AsyncMock(return_value="not ready enough"),
    )

    with pytest.raises(ValueError, match="not ready enough"):
        await run_session_viability_probe(db_session, device, checked_by="manual")


async def test_run_session_viability_probe_changed_state_and_health_handler_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.available)
    relocked = MagicMock(id=device.id, operational_state=DeviceOperationalState.offline)
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.settings_service, "get", lambda key: 5)
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    monkeypatch.setattr(session_viability, "set_operational_state", AsyncMock())
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability, "probe_session_via_agent_node", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(session_viability, "_write_session_viability", AsyncMock(return_value={"status": "failed"}))
    handler = AsyncMock()
    session_viability.configure_health_failure_handler(handler)
    try:
        state = await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
        )
    finally:
        session_viability.configure_health_failure_handler(None)

    assert state == {"status": "failed"}
    assert device.device_config == {}
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["reason"] == "Appium session viability probe failed"


async def test_run_session_viability_probe_restores_previous_state_on_exception(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.offline)
    relocked = MagicMock(id=device.id, operational_state=DeviceOperationalState.busy)
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.settings_service, "get", lambda key: 5)
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    set_state = AsyncMock()
    monkeypatch.setattr(session_viability, "set_operational_state", set_state)
    monkeypatch.setattr(
        session_viability.capability_service,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    monkeypatch.setattr(
        session_viability,
        "ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.available),
    )

    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.recovery,
        )

    assert set_state.await_args_list[-1].args[1] == DeviceOperationalState.offline


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
    monkeypatch.setattr(session_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.settings_service, "get", lambda key: 5)
    monkeypatch.setattr(session_viability, "_write_session_viability", AsyncMock(return_value={"status": "failed"}))

    state = await run_session_viability_probe(
        fake_db,
        no_node,
        checked_by=session_viability.SessionViabilityCheckedBy.manual,
    )

    assert state["status"] == "failed"
    assert no_node.device_config == {}
    assert fake_db.commit.await_count >= 2

    device_id = uuid.uuid4()
    available = MagicMock(id=device_id, operational_state=DeviceOperationalState.available, hold=None)
    available.appium_node = MagicMock(observed_running=True)
    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.available)
    relocked = MagicMock(id=device_id, operational_state=DeviceOperationalState.busy)
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    set_state = AsyncMock()
    monkeypatch.setattr(session_viability, "set_operational_state", set_state)
    monkeypatch.setattr(
        session_viability.capability_service,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    monkeypatch.setattr(
        session_viability,
        "ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.available),
    )

    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            available,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
        )

    assert set_state.await_args_list[-1].args[1] != DeviceOperationalState.offline
