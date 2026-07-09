"""Unit tests for the shared remediation-escalation ladder (no DB needed)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.devices.services.lifecycle_policy_state import default_state, now
from app.lifecycle.services.escalation import backoff_active, escalate_remediation_failure
from tests.fakes import FakeSettingsReader

SETTINGS = FakeSettingsReader(
    {
        "general.lifecycle_recovery_backoff_base_sec": 60,
        "general.lifecycle_recovery_backoff_max_sec": 900,
        "general.lifecycle_recovery_review_threshold": 3,
    }
)


def test_backoff_active_none_when_unset() -> None:
    assert backoff_active(default_state()) is None


def test_backoff_active_returns_future_deadline() -> None:
    state = default_state()
    state["backoff_until"] = (now() + timedelta(seconds=60)).isoformat()
    assert backoff_active(state) is not None


def test_backoff_active_none_when_expired() -> None:
    state = default_state()
    state["backoff_until"] = (now() - timedelta(seconds=1)).isoformat()
    assert backoff_active(state) is None


async def test_escalate_increments_attempts_and_arms_backoff() -> None:
    review = AsyncMock()
    device = SimpleNamespace(review_required=False)
    state = default_state()
    first = await escalate_remediation_failure(
        None, device, state, settings=SETTINGS, review=review, source="t", reason="r"
    )
    second = await escalate_remediation_failure(
        None, device, state, settings=SETTINGS, review=review, source="t", reason="r"
    )
    assert (first.attempts, second.attempts) == (1, 2)
    assert state["recovery_backoff_attempts"] == 2
    assert backoff_active(state) is not None
    assert first.shelved is False and second.shelved is False
    review.mark_review_required.assert_not_called()


async def test_escalate_promotes_to_review_at_threshold() -> None:
    review = AsyncMock()
    device = SimpleNamespace(review_required=False)
    state = default_state()
    state["recovery_backoff_attempts"] = 2
    outcome = await escalate_remediation_failure(
        None, device, state, settings=SETTINGS, review=review, source="node_health", reason="kept failing"
    )
    assert outcome.shelved is True
    review.mark_review_required.assert_awaited_once_with(None, device, reason="kept failing", source="node_health")
