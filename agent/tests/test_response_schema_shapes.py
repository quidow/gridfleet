"""Response schemas accept the dicts the routes currently return."""

from __future__ import annotations

from agent_app.appium.schemas import (
    AppiumLogsResponse,
    AppiumReconfigureResponse,
    AppiumStartResponse,
    AppiumStatusResponse,
    AppiumStopResponse,
)
from agent_app.host.schemas import HealthResponse, HostTelemetryResponse
from agent_app.pack.schemas import (
    FeatureActionResponse,
    PackDeviceHealthResponse,
    PackDeviceLifecycleResponse,
    PackDevicePropertiesResponse,
    PackDevicesResponse,
    PackDeviceTelemetryResponse,
)
from agent_app.plugins.schemas import PluginListItem, PluginSyncResponse
from agent_app.tools.schemas import ToolsStatusResponse


def test_appium_start_response_accepts_route_shape() -> None:
    AppiumStartResponse.model_validate({"pid": 1234, "port": 4723, "connection_target": "device-1"})


def test_appium_reconfigure_response_accepts_route_shape() -> None:
    AppiumReconfigureResponse.model_validate(
        {"port": 4723, "accepting_new_sessions": True, "stop_pending": False, "grid_run_id": None}
    )


def test_appium_stop_response_accepts_route_shape() -> None:
    AppiumStopResponse.model_validate({"stopped": True, "port": 4723})


def test_appium_status_response_accepts_arbitrary_status_dict() -> None:
    AppiumStatusResponse.model_validate({"port": 4723, "pid": 1, "running": True})


def test_appium_logs_response_accepts_route_shape() -> None:
    AppiumLogsResponse.model_validate({"port": 4723, "lines": ["[info] ready"], "count": 1})


def test_health_response_accepts_route_shape() -> None:
    HealthResponse.model_validate(
        {
            "status": "ok",
            "hostname": "host-1",
            "os_type": "linux",
            "version": "0.1.0",
            "missing_prerequisites": [],
            "capabilities": {},
            "appium_processes": {"running_nodes": [], "recent_restart_events": []},
            "version_guidance": {},
        }
    )


def test_host_telemetry_response_accepts_route_shape() -> None:
    HostTelemetryResponse.model_validate({"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0})


def test_pack_devices_response_accepts_route_shape() -> None:
    PackDevicesResponse.model_validate({"candidates": []})


def test_pack_device_properties_response_accepts_arbitrary_dict() -> None:
    PackDevicePropertiesResponse.model_validate({"foo": "bar"})


def test_pack_device_health_response_accepts_route_shape() -> None:
    PackDeviceHealthResponse.model_validate(
        {
            "healthy": None,
            "checks": [{"check_id": "adapter_unavailable", "ok": False, "message": "no adapter"}],
        }
    )


def test_pack_device_telemetry_response_accepts_arbitrary_dict() -> None:
    PackDeviceTelemetryResponse.model_validate({"cpu": 0.1})


def test_pack_device_lifecycle_response_accepts_route_shape() -> None:
    PackDeviceLifecycleResponse.model_validate({"success": False, "detail": "no adapter"})


def test_feature_action_response_accepts_route_shape() -> None:
    FeatureActionResponse.model_validate({"ok": True, "detail": "done", "data": {}})


def test_plugin_list_item_accepts_route_shape() -> None:
    PluginListItem.model_validate({"name": "appium-some-plugin", "version": "1.2.3"})


def test_plugin_sync_response_accepts_arbitrary_dict() -> None:
    PluginSyncResponse.model_validate({"installed": [], "removed": []})


def test_tools_status_response_accepts_arbitrary_dict() -> None:
    ToolsStatusResponse.model_validate({"adb": "1.0", "node": "20.0"})
