from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.services.session_viability import (
    _check_due_devices,
    get_session_viability,
    get_session_viability_control_plane_state,
    probe_session_via_grid,
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

    node = AppiumNode(device_id=device.id, port=4729, grid_url="http://hub:4444", state=NodeState.stopped)
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

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
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

    node = AppiumNode(device_id=device.id, port=4733, grid_url="http://hub:4444", state=NodeState.running)
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
        state=NodeState.running,
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
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("", request=request))

    with patch("app.services.session_viability.httpx.AsyncClient", return_value=mock_client):
        ok, error = await probe_session_via_grid({"platformName": "iOS"}, timeout_sec=5)

    assert ok is False
    assert error == "Session create request failed: ReadTimeout while calling http://hub:4444/session"


async def test_probe_session_via_grid_preserves_configured_base_path() -> None:
    create_response = MagicMock(spec=httpx.Response, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-1"}}
    delete_response = MagicMock(spec=httpx.Response, status_code=200)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=create_response)
    mock_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch("app.services.session_viability.settings_service.get", return_value="http://hub:4444/wd/hub"),
        patch("app.services.session_viability.httpx.AsyncClient", return_value=mock_client) as client_factory,
    ):
        ok, error = await probe_session_via_grid({"platformName": "iOS"}, timeout_sec=5)

    assert ok is True
    assert error is None
    client_factory.assert_called_once_with(base_url=httpx.URL("http://hub:4444/wd/hub"), timeout=5)
    mock_client.post.assert_awaited_once_with(
        "session", json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}, "firstMatch": [{}]}}
    )
    mock_client.delete.assert_awaited_once_with("session/session-1")
