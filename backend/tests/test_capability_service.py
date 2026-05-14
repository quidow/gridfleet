from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import common as node_service_common
from app.devices.models import ConnectionType, Device, DeviceType
from app.devices.services import capability as capability_service


def _device(
    *,
    platform_id: str = "android_mobile",
    pack_id: str = "appium-uiautomator2",
    device_type: DeviceType = DeviceType.real_device,
    connection_target: str = "serial-1",
    ip_address: str | None = None,
) -> Device:
    return Device(
        id=uuid4(),
        host_id=uuid4(),
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=connection_target,
        connection_target=connection_target,
        name="Demo Device",
        os_version="17.0",
        device_type=device_type,
        connection_type=ConnectionType.usb,
        ip_address=ip_address,
    )


def test_build_capabilities_includes_platform_specific_fields() -> None:
    device = _device(platform_id="ios", pack_id="appium-xcuitest")

    caps = capability_service.build_capabilities(
        device,
        "XCUITest",
        appium_platform_name="iOS",
        session_caps={"appium:noReset": True, "custom": "value"},
    )

    assert caps["platformName"] == "iOS"
    assert caps["appium:udid"] == "serial-1"
    assert caps["appium:deviceName"] == "Demo Device"
    assert caps["appium:automationName"] == "XCUITest"
    assert caps["appium:gridfleet:deviceId"] == str(device.id)
    assert caps["appium:noReset"] is True
    assert caps["custom"] == "value"


def test_config_appium_caps_cannot_override_manager_owned_routing_caps() -> None:
    device = _device(connection_target="serial-1")
    device.name = "Trusted Device"
    device.device_config = {
        "appium_caps": {
            "appium:noReset": True,
        }
    }

    caps = capability_service.build_capabilities(
        device,
        None,
        appium_platform_name="Android",
        session_caps={
            "platformName": "iOS",
            "appium:udid": "wrong-serial",
            "appium:deviceName": "Wrong Name",
            "appium:gridfleet:deviceId": "wrong-id",
            "appium:gridfleet:deviceName": "Wrong Grid Name",
            "appium:noReset": True,
        },
    )

    assert caps["platformName"] == "Android"
    assert caps["appium:udid"] == "serial-1"
    assert caps["appium:deviceName"] == "Trusted Device"
    assert caps["appium:gridfleet:deviceId"] == str(device.id)
    assert caps["appium:noReset"] is True

    extra_caps = node_service_common.build_extra_caps(device)
    stereotype_caps = node_service_common.build_grid_stereotype_caps(device)
    assert extra_caps["appium:gridfleet:deviceId"] == str(device.id)
    assert extra_caps["appium:gridfleet:deviceName"] == "Trusted Device"
    assert extra_caps["appium:noReset"] is True
    assert stereotype_caps["appium:gridfleet:deviceId"] == str(device.id)
    assert "appium:udid" not in stereotype_caps


def test_build_capabilities_handles_roku_tvos_and_simulator() -> None:
    roku = _device(platform_id="roku_network", pack_id="appium-roku", ip_address="10.0.0.50")
    roku.device_config = {"roku_password": "secret", "appium_caps": None}
    tvos = _device(
        platform_id="tvos",
        pack_id="appium-xcuitest",
        device_type=DeviceType.real_device,
        ip_address="10.0.0.51",
    )
    simulator = _device(
        platform_id="ios",
        pack_id="appium-xcuitest",
        device_type=DeviceType.simulator,
        connection_target="sim-1",
    )

    # Roku ip/password are now injected via session_caps from the catalog (render_device_field_capabilities +
    # render_default_capabilities). Pass them as session_caps to simulate what get_device_capabilities() does.
    # appium_platform_name is resolved from the pack manifest by the async get_device_capabilities path;
    # for direct build_capabilities calls we pass it explicitly.
    roku_caps = capability_service.build_capabilities(
        roku,
        None,
        appium_platform_name="Roku",
        session_caps={"appium:ip": "10.0.0.50", "appium:password": "secret"},
    )
    tvos_caps = capability_service.build_capabilities(
        tvos,
        None,
        appium_platform_name="tvOS",
        session_caps={"appium:wdaBaseUrl": "http://10.0.0.51"},
    )
    simulator_caps = capability_service.build_capabilities(simulator, None, appium_platform_name="iOS")

    assert roku_caps["platformName"] == "Roku"
    assert roku_caps["appium:ip"] == "10.0.0.50"
    assert roku_caps["appium:password"] == "secret"
    assert tvos_caps["appium:wdaBaseUrl"] == "http://10.0.0.51"
    assert simulator_caps["appium:simulatorRunning"] is True


def test_appium_udid_prefers_active_target_for_running_android_emulator() -> None:
    device = _device(device_type=DeviceType.emulator, connection_target="Pixel_8")
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )

    assert capability_service._appium_udid_for_capabilities(device, "emulator-5554") == "emulator-5554"
    assert capability_service._appium_udid_for_capabilities(device, None) == "Pixel_8"


async def test_active_target_from_host_snapshot_matches_port() -> None:
    db = AsyncMock()
    device = _device(device_type=DeviceType.emulator)
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )

    with patch(
        "app.devices.services.capability.control_plane_state_store.get_value",
        new=AsyncMock(
            return_value={"running_nodes": [{"port": 4700}, {"port": 4723, "connection_target": "emulator-5554"}]}
        ),
    ):
        result = await capability_service._active_target_from_host_snapshot(db, device)

    assert result == "emulator-5554"


async def test_active_target_from_host_snapshot_returns_none_for_invalid_snapshot() -> None:
    db = AsyncMock()
    device = _device()
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )

    with patch("app.devices.services.capability.control_plane_state_store.get_value", new=AsyncMock(return_value=[])):
        assert await capability_service._active_target_from_host_snapshot(db, device) is None

    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    with patch(
        "app.devices.services.capability.control_plane_state_store.get_value",
        new=AsyncMock(return_value={"running_nodes": [{"port": 9999, "connection_target": "emulator-1"}]}),
    ):
        assert await capability_service._active_target_from_host_snapshot(db, device) is None


async def test_get_live_active_connection_target_uses_node_value_or_snapshot() -> None:
    db = AsyncMock()
    device = _device(device_type=DeviceType.emulator)
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="emulator-5554",
    )

    assert await capability_service._get_live_active_connection_target(db, device) == "emulator-5554"
    db.flush.assert_not_awaited()

    # When there is no cached target, the helper must acquire Device + AppiumNode
    # locks and persist the value returned by the host-snapshot probe.
    locked_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target=None,
    )
    device.appium_node.active_connection_target = None
    with (
        patch(
            "app.devices.services.capability._active_target_from_host_snapshot",
            new=AsyncMock(return_value="emulator-5556"),
        ),
        patch("app.devices.locking.lock_device", new=AsyncMock()),
        patch(
            "app.appium_nodes.services.locking.lock_appium_node_for_device",
            new=AsyncMock(return_value=locked_node),
        ),
    ):
        assert await capability_service._get_live_active_connection_target(db, device) == "emulator-5556"
    assert locked_node.active_connection_target == "emulator-5556"
    db.flush.assert_awaited_once()


async def test_get_live_active_connection_target_skips_non_emulator() -> None:
    db = AsyncMock()
    device = _device()
    assert await capability_service._get_live_active_connection_target(db, device) is None

    emulator = _device(device_type=DeviceType.emulator)
    emulator.appium_node = AppiumNode(
        device_id=emulator.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    with (
        patch("app.devices.services.capability._active_target_from_host_snapshot", new=AsyncMock(return_value=None)),
        patch("app.devices.locking.lock_device", new=AsyncMock()),
        patch("app.appium_nodes.services.locking.lock_appium_node_for_device", new=AsyncMock(return_value=None)),
    ):
        assert await capability_service._get_live_active_connection_target(db, emulator) is None


async def test_get_device_capabilities_fetches_driver_and_session_overrides() -> None:
    db = AsyncMock()
    device = _device()
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )

    with (
        patch(
            "app.devices.services.capability.render_stereotype",
            new=AsyncMock(return_value={"appium:automationName": "UiAutomator2"}),
        ),
        patch(
            "app.appium_nodes.services.resource_service.get_capabilities",
            new=AsyncMock(return_value={"appium:systemPort": 8200}),
        ),
        patch(
            "app.devices.services.capability._get_live_active_connection_target",
            new=AsyncMock(return_value="serial-1"),
        ),
        patch(
            "app.devices.services.capability.resolve_pack_platform",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    appium_platform_name="Android",
                    parallel_resources=SimpleNamespace(ports=[]),
                    default_capabilities={},
                    device_fields_schema=[],
                )
            ),
        ),
        patch("app.devices.services.capability.appium_capability_keys.sanitize_appium_caps", return_value={}),
    ):
        caps = await capability_service.get_device_capabilities(db, device)

    assert caps["appium:automationName"] == "UiAutomator2"
    assert caps["appium:systemPort"] == 8200


async def test_get_device_capabilities_raises_when_pack_platform_is_missing() -> None:
    db = AsyncMock()
    device = _device()

    with (
        patch(
            "app.devices.services.capability.render_stereotype",
            new=AsyncMock(return_value={"appium:automationName": "UiAutomator2"}),
        ),
        patch(
            "app.devices.services.capability.resolve_pack_platform",
            new=AsyncMock(side_effect=LookupError),
        ),
    ):
        try:
            await capability_service.get_device_capabilities(db, device)
        except LookupError:
            return

    raise AssertionError("expected missing pack platform lookup to propagate")
