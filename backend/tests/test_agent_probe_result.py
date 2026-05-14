"""Tests for ProbeResult - typed projection of agent status responses."""

from __future__ import annotations

from app.agent_comm.probe_result import from_status_response


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
