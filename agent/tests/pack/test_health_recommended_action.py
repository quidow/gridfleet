from __future__ import annotations

from agent_app.pack.adapter_types import HealthCheckResult
from agent_app.pack.dispatch import _adapter_health_payload


def test_payload_lifts_recommended_action_to_top_level() -> None:
    results = [
        HealthCheckResult(check_id="adb_connected", ok=False, detail="down"),
        HealthCheckResult(check_id="link_repairable", ok=False, recommended_action="reconnect"),
    ]
    payload = _adapter_health_payload(results)
    assert payload["recommended_action"] == "reconnect"
    assert payload["healthy"] is False


def test_payload_has_no_recommended_action_key_when_none() -> None:
    results = [HealthCheckResult(check_id="adb_connected", ok=True)]
    payload = _adapter_health_payload(results)
    assert "recommended_action" not in payload
