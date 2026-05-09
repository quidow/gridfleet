from __future__ import annotations

import dataclasses

import pytest

from app.services.heartbeat_outcomes import (
    ClientMode,
    HeartbeatOutcome,
    HeartbeatPingResult,
)


def test_outcome_enum_values() -> None:
    expected = {
        "success",
        "timeout",
        "connect_error",
        "dns_error",
        "http_error",
        "invalid_payload",
        "circuit_open",
        "unexpected_error",
    }
    assert {member.value for member in HeartbeatOutcome} == expected


def test_client_mode_enum_values() -> None:
    assert {member.value for member in ClientMode} == {"pooled", "fresh", "skipped_circuit_open"}


def test_result_alive_property() -> None:
    success = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={"status": "ok"},
        duration_ms=12,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )
    fail = HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=5_000,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ReadTimeout",
    )
    assert success.alive is True
    assert fail.alive is False


@pytest.mark.parametrize(
    "outcome",
    [o for o in HeartbeatOutcome if o is not HeartbeatOutcome.success],
)
def test_alive_is_false_for_all_failure_outcomes(outcome: HeartbeatOutcome) -> None:
    result = HeartbeatPingResult(
        outcome=outcome,
        payload=None,
        duration_ms=1,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category=None,
    )
    assert result.alive is False


def test_result_is_frozen() -> None:
    result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={},
        duration_ms=1,
        client_mode=ClientMode.fresh,
        http_status=200,
        error_category=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.duration_ms = 2  # type: ignore[misc]
