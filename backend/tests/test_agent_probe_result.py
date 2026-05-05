"""Tests for ProbeResult - typed projection of agent probe responses."""

from __future__ import annotations

from app.services.agent_probe_result import (
    from_probe_session_response,
    from_status_response,
)


def test_status_2xx_running_true_is_ack() -> None:
    result = from_status_response({"running": True})
    assert result.status == "ack"


def test_status_2xx_running_false_is_refused() -> None:
    result = from_status_response({"running": False})
    assert result.status == "refused"
    assert result.detail == "Appium not running"


def test_status_none_payload_is_indeterminate() -> None:
    result = from_status_response(None)
    assert result.status == "indeterminate"


def test_probe_session_ok_true_is_ack() -> None:
    result = from_probe_session_response((True, None))
    assert result.status == "ack"


def test_probe_session_appium_side_failure_is_refused() -> None:
    result = from_probe_session_response((False, "Probe session returned an invalid payload"))
    assert result.status == "refused"
    assert result.detail == "Probe session returned an invalid payload"


def test_probe_session_http_layer_failure_is_indeterminate() -> None:
    result = from_probe_session_response((False, "Probe session failed (HTTP 503)"))
    assert result.status == "indeterminate"
