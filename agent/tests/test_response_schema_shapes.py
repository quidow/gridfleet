"""Response schemas accept the dicts the routes currently return."""

from __future__ import annotations

from agent_app.appium.schemas import (
    AppiumLogsResponse,
    AppiumStatusResponse,
)
from agent_app.host.schemas import HealthResponse, HostTelemetryResponse
from agent_app.pack.schemas import (
    PackDeviceHealthResponse,
    PackDeviceLifecycleResponse,
    PackDevicesResponse,
)
from agent_app.tools.schemas import ToolsStatusResponse


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
            "registered": True,
            "missing_prerequisites": [],
            "capabilities": {},
            "appium_processes": {"running_nodes": [], "recent_restart_events": []},
            "version_guidance": {},
        }
    )


def test_host_telemetry_response_accepts_route_shape() -> None:
    HostTelemetryResponse.model_validate(
        {
            "recorded_at": "2024-01-01T00:00:00+00:00",
            "cpu_percent": 12.5,
            "memory_used_mb": 4096,
            "memory_total_mb": 16384,
            "disk_used_gb": 50.0,
            "disk_total_gb": 500.0,
            "disk_percent": 10.0,
        }
    )


def test_pack_devices_response_accepts_route_shape() -> None:
    PackDevicesResponse.model_validate({"candidates": []})


def test_pack_device_health_response_accepts_route_shape() -> None:
    PackDeviceHealthResponse.model_validate(
        {
            "healthy": None,
            "checks": [{"check_id": "adapter_unavailable", "ok": False, "message": "no adapter"}],
        }
    )


def test_pack_device_lifecycle_response_accepts_route_shape() -> None:
    PackDeviceLifecycleResponse.model_validate({"success": False, "detail": "no adapter", "extras": {}})


def test_tools_status_response_accepts_structured_dict() -> None:
    ToolsStatusResponse.model_validate(
        {
            "host": {
                "node": {"name": "Node", "version": "20.0", "description": "JavaScript runtime for Appium server"},
            },
            "packs": {
                "test-pack": [
                    {"name": "adb", "version": "1.0", "description": "ADB tool"},
                ],
            },
        }
    )
