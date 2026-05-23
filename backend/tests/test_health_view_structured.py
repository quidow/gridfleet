from unittest.mock import MagicMock

from app.devices.services.health_view import build_public_summary


def _mock_device(
    *,
    checks_healthy: bool | None = None,
    checks_summary: str | None = None,
    checks_checked_at: object = None,
    viability_status: str | None = None,
    viability_error: str | None = None,
    viability_checked_at: object = None,
    node_pid: int | None = None,
    node_target: str | None = None,
    node_health_running: bool | None = None,
    node_health_state: str | None = None,
    node_last_checked: object = None,
) -> MagicMock:
    device = MagicMock()
    device.device_checks_healthy = checks_healthy
    device.device_checks_summary = checks_summary
    device.device_checks_checked_at = checks_checked_at
    device.session_viability_status = viability_status
    device.session_viability_error = viability_error
    device.session_viability_checked_at = viability_checked_at
    if node_pid is not None or node_health_running is not None:
        node = MagicMock()
        node.pid = node_pid
        node.active_connection_target = node_target
        node.health_running = node_health_running
        node.health_state = node_health_state
        node.last_health_checked_at = node_last_checked
        device.appium_node = node
    else:
        device.appium_node = None
    return device


def test_structured_fields_all_healthy() -> None:
    device = _mock_device(
        checks_healthy=True,
        checks_summary="Healthy",
        node_pid=100,
        node_target="device-1",
        node_health_running=True,
        node_health_state="running",
        viability_status="passed",
    )
    summary = build_public_summary(device)
    assert summary["connectivity_status"] == "ok"
    assert summary["node_status"] == "running"
    assert summary["session_status"] == "passed"


def test_structured_fields_connectivity_failed() -> None:
    device = _mock_device(checks_healthy=False, checks_summary="ADB not responsive")
    summary = build_public_summary(device)
    assert summary["connectivity_status"] == "failed"
    assert summary["node_status"] is None
    assert summary["session_status"] is None


def test_structured_fields_node_stopped() -> None:
    device = _mock_device(
        checks_healthy=True,
        node_health_running=False,
        node_health_state="stopped",
    )
    summary = build_public_summary(device)
    assert summary["node_status"] == "stopped"


def test_structured_fields_session_failed() -> None:
    device = _mock_device(
        checks_healthy=True,
        viability_status="failed",
        viability_error="Could not start session",
    )
    summary = build_public_summary(device)
    assert summary["session_status"] == "failed"


def test_structured_fields_no_signals() -> None:
    device = _mock_device()
    summary = build_public_summary(device)
    assert summary["connectivity_status"] is None
    assert summary["node_status"] is None
    assert summary["session_status"] is None
