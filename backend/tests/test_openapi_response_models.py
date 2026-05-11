"""Public API responses used by frontend type generation must be named schemas."""

from __future__ import annotations

from app.main import app


def _response_ref(path: str, method: str = "get", status: str = "200") -> str:
    schema = app.openapi()["paths"][path][method]["responses"][status]["content"]["application/json"]["schema"]
    ref = schema.get("$ref")
    assert isinstance(ref, str)
    return ref


def _response_item_ref(path: str, method: str = "get", status: str = "200") -> str:
    schema = app.openapi()["paths"][path][method]["responses"][status]["content"]["application/json"]["schema"]
    ref = schema.get("items", {}).get("$ref")
    assert isinstance(ref, str)
    return ref


def test_public_anonymous_responses_have_named_openapi_components() -> None:
    schema = app.openapi()
    components = schema["components"]["schemas"]
    expected_components = {
        "GridStatusRead",
        "GridQueueRead",
        "HealthStatusRead",
        "LiveHealthRead",
        "DeviceConfigRead",
        "ConfigAuditEntryRead",
        "DeviceHealthRead",
        "SessionViabilityRead",
        "TestDataRead",
        "TestDataAuditEntryRead",
        "HostToolStatusRead",
        "ToolEnsureResultItemRead",
        "HostToolEnsureResultRead",
    }
    assert expected_components <= set(components)

    assert _response_ref("/api/grid/status") == "#/components/schemas/GridStatusRead"
    assert _response_ref("/api/grid/queue") == "#/components/schemas/GridQueueRead"
    assert _response_ref("/health/live") == "#/components/schemas/LiveHealthRead"
    assert _response_ref("/health/ready") == "#/components/schemas/HealthStatusRead"
    assert _response_ref("/api/health") == "#/components/schemas/HealthStatusRead"
    assert _response_ref("/api/devices/{device_id}/config") == "#/components/schemas/DeviceConfigRead"
    assert _response_item_ref("/api/devices/{device_id}/config/history") == "#/components/schemas/ConfigAuditEntryRead"
    assert _response_ref("/api/devices/{device_id}/health") == "#/components/schemas/DeviceHealthRead"
    assert _response_ref("/api/devices/{device_id}/session-test", method="post") == (
        "#/components/schemas/SessionViabilityRead"
    )
    assert _response_ref("/api/devices/{device_id}/test_data") == "#/components/schemas/TestDataRead"
    assert _response_item_ref("/api/devices/{device_id}/test_data/history") == (
        "#/components/schemas/TestDataAuditEntryRead"
    )
    assert _response_ref("/api/hosts/{host_id}/tools/status") == "#/components/schemas/HostToolStatusRead"


def test_appium_node_read_exposes_desired_state_fields() -> None:
    schema = app.openapi()
    appium = schema["components"]["schemas"]["AppiumNodeRead"]
    properties = appium["properties"]
    assert {
        "desired_state",
        "desired_port",
        "transition_token",
        "transition_deadline",
        "last_observed_at",
    } <= properties.keys()
    required = set(appium.get("required", []))
    assert "desired_state" in required
    assert "desired_port" not in required
    assert "transition_token" not in required

    desired_ref = properties["desired_state"]["$ref"]
    desired_schema = schema["components"]["schemas"][desired_ref.rsplit("/", 1)[-1]]
    assert desired_schema["enum"] == ["running", "stopped"]


def test_appium_node_read_exposes_effective_state() -> None:
    schema = app.openapi()
    appium = schema["components"]["schemas"]["AppiumNodeRead"]
    assert "effective_state" in appium["properties"]


def test_appium_node_read_does_not_expose_legacy_state() -> None:
    schema = app.openapi()
    appium = schema["components"]["schemas"]["AppiumNodeRead"]
    assert "state" not in appium["properties"], "Phase 6: legacy 'state' field must not appear in the API"
