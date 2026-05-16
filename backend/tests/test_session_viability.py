import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.hosts.models import Host
from app.sessions import service_viability as session_viability
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY
from app.sessions.service_viability import (
    _check_due_devices,
    _extract_session_error,
    _format_http_error,
    _get_grid_probe_client,
    _parse_timestamp,
    _should_run_scheduled_probe,
    get_session_viability,
    get_session_viability_control_plane_state,
    grid_probe_response_to_result,
    probe_session_via_grid,
    record_session_viability_result,
    run_session_viability_probe,
    set_session_viability_control_plane_entry,
)
from app.settings import settings_service

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
            "app.sessions.service_viability.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.sessions.service_viability.probe_session_via_grid",
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
            "app.sessions.service_viability.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.sessions.service_viability.probe_session_via_grid",
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

    with patch(
        "app.sessions.service_viability.probe_session_via_grid",
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
    assert probe_mock.await_args.kwargs["grid_url"] == "http://node-grid:4444/wd/hub"


async def test_run_session_viability_probe_writes_probe_row_on_ack(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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

    with patch(
        "app.sessions.service_viability.probe_session_via_grid",
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

    with patch(
        "app.sessions.service_viability.probe_session_via_grid",
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

    with patch("app.sessions.service_viability.run_session_viability_probe", new_callable=AsyncMock) as mock_probe:
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

    with patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client):
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
        patch("app.sessions.service_viability.settings_service.get", return_value="http://hub:4444/wd/hub"),
        patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client),
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


async def test_probe_session_via_grid_uses_explicit_node_grid_url() -> None:
    create_response = MagicMock(spec=httpx.Response, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-1"}}
    delete_response = MagicMock(spec=httpx.Response, status_code=200)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=create_response)
    mock_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch("app.sessions.service_viability.settings_service.get", return_value="http://global-hub:4444"),
        patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid(
            {"platformName": "iOS"},
            timeout_sec=5,
            grid_url="http://node-hub:4444/wd/hub",
        )

    assert ok is True
    assert error is None
    mock_client.post.assert_awaited_once_with(
        "http://node-hub:4444/wd/hub/session",
        json={"capabilities": {"alwaysMatch": {"platformName": "iOS"}, "firstMatch": [{}]}},
        timeout=5,
    )
    mock_client.delete.assert_awaited_once_with("http://node-hub:4444/wd/hub/session/session-1", timeout=5)


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
        patch("app.sessions.service_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client),
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
        patch("app.sessions.service_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "Android"}, timeout_sec=3)

    assert ok is False
    assert error == "Session created but cleanup failed (500)"

    mock_client.delete = AsyncMock(side_effect=httpx.ConnectError("down"))
    with (
        patch("app.sessions.service_viability.settings_service.get", return_value="http://hub:4444"),
        patch("app.sessions.service_viability._get_grid_probe_client", return_value=mock_client),
    ):
        ok, error = await probe_session_via_grid({"platformName": "Android"}, timeout_sec=3)

    assert ok is False
    assert error == "Session created but cleanup failed: down"


async def test_run_session_viability_probe_rejects_missing_running_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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
    monkeypatch.setattr(
        session_viability.settings_service,
        "get",
        lambda key: 1 if "failure_threshold" in key else 5,
    )
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    monkeypatch.setattr(session_viability, "set_operational_state", AsyncMock())
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(
        session_viability,
        "_write_session_viability",
        AsyncMock(return_value={"status": "failed", "consecutive_failures": 1}),
    )
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

    assert state == {"status": "failed", "consecutive_failures": 1}
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

    monkeypatch.setattr(session_viability.settings_service, "get", _settings)
    monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(False, error)))
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", AsyncMock(return_value={}))
    if handler is not None:
        session_viability.configure_health_failure_handler(handler)
    return await run_session_viability_probe(
        db, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
    )


def _make_viability_device(db_host: Host, suffix: str) -> tuple[Device, AppiumNode]:
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
        session_viability.configure_health_failure_handler(None)

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
            device.operational_state = DeviceOperationalState.available
            await _run_failing_probe(db_session, device, monkeypatch, error="grid hiccup", threshold=3, handler=handler)
    finally:
        session_viability.configure_health_failure_handler(None)

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

    monkeypatch.setattr(session_viability.settings_service, "get", _settings)
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", AsyncMock(return_value={}))
    session_viability.configure_health_failure_handler(handler)
    try:
        # Two consecutive failures get to count=2.
        monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(False, "transient")))
        for _ in range(2):
            device.operational_state = DeviceOperationalState.available
            await run_session_viability_probe(
                db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
            )

        # A passing probe must reset the counter back to 0.
        monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(True, None)))
        device.operational_state = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
        mid = await get_session_viability(db_session, device)
        assert mid is not None and mid["consecutive_failures"] == 0

        # One more failure must start the count over, not jump straight to threshold.
        monkeypatch.setattr(
            session_viability, "probe_session_via_grid", AsyncMock(return_value=(False, "transient again"))
        )
        device.operational_state = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
    finally:
        session_viability.configure_health_failure_handler(None)

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

    monkeypatch.setattr(session_viability.settings_service, "get", _settings)
    monkeypatch.setattr(session_viability.capability_service, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(False, grid_error)))

    await run_session_viability_probe(
        db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
    )

    persisted = await get_session_viability(db_session, device)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error_category"] == "grid_no_slot"

    # A passing probe must clear ``error_category`` so a recovered device does
    # not keep an old infra tag attached.
    monkeypatch.setattr(session_viability, "probe_session_via_grid", AsyncMock(return_value=(True, None)))
    device.operational_state = DeviceOperationalState.available
    await run_session_viability_probe(
        db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
    )
    after_pass = await get_session_viability(db_session, device)
    assert after_pass is not None
    assert after_pass["status"] == "passed"
    assert after_pass["error_category"] is None
