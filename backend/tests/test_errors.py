from __future__ import annotations

from app.errors import AgentCallError, AgentResponseError, AgentUnreachableError, CircuitOpenError


def test_agent_response_error_is_agent_call_error_but_not_unreachable() -> None:
    err = AgentResponseError("host-a", "boom", http_status=503)
    assert isinstance(err, AgentCallError)
    assert not isinstance(err, AgentUnreachableError)
    assert not isinstance(err, CircuitOpenError)
    assert err.error_code == "AGENT_RESPONSE_ERROR"
    assert err.status_code == 502
    assert err.http_status == 503
    assert err.host == "host-a"
    assert err.details["host"] == "host-a"
    assert err.details["http_status"] == 503


def test_agent_response_error_without_status() -> None:
    err = AgentResponseError("host-b", "transport boom")
    assert err.http_status is None
    assert "http_status" not in err.details
