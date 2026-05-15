"""Verify response schemas keep open-ended fields under named envelopes."""

from __future__ import annotations

from typing import Any

from agent_app.main import app

OPEN_RESPONSE_SCHEMAS = {
    "AppiumStatusResponse",
    "HealthCheckResult",
    "HealthResponse",
    "HostTelemetryResponse",
    "PackDeviceCandidate",
    "PackDeviceHealthResponse",
    "PackDeviceLifecycleResponse",
    "PackDevicePropertiesResponse",
    "PackDeviceTelemetryResponse",
    "PackDevicesResponse",
}


def test_response_components_do_not_allow_root_extras() -> None:
    spec: dict[str, Any] = app.openapi()
    schemas: dict[str, Any] = spec["components"]["schemas"]
    offenders = [
        name for name in sorted(OPEN_RESPONSE_SCHEMAS) if schemas.get(name, {}).get("additionalProperties") is True
    ]
    assert offenders == []
