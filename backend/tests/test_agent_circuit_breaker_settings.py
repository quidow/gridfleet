from __future__ import annotations

import pytest

from app.services.agent_circuit_breaker import AgentCircuitBreaker
from app.services.settings_service import settings_service


@pytest.mark.asyncio
async def test_breaker_uses_runtime_settings_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    overrides = {
        "agent.circuit_breaker_failure_threshold": 2,
        "agent.circuit_breaker_cooldown_seconds": 7,
    }
    monkeypatch.setattr(settings_service, "get", lambda key: overrides[key])
    breaker = AgentCircuitBreaker()

    await breaker.record_failure("h1", error="boom")
    await breaker.record_failure("h1", error="boom")  # threshold=2 → opens here
    snapshot = breaker.snapshot("h1")
    assert snapshot["status"] == "open"


@pytest.mark.asyncio
async def test_breaker_picks_up_changed_threshold_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    overrides = {
        "agent.circuit_breaker_failure_threshold": 5,
        "agent.circuit_breaker_cooldown_seconds": 30,
    }
    monkeypatch.setattr(settings_service, "get", lambda key: overrides[key])
    breaker = AgentCircuitBreaker()
    await breaker.record_failure("h1", error="boom")
    overrides["agent.circuit_breaker_failure_threshold"] = 1  # tighten mid-flight
    await breaker.record_failure("h1", error="boom")
    snapshot = breaker.snapshot("h1")
    assert snapshot["status"] == "open"
