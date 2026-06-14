"""Pure-function tests for Appium node effective-state derivation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.appium_nodes.services.effective_state import compute_effective_state

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def test_restarting_when_transition_active() -> None:
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=True,
            health_state=None,
            transition_token=uuid.uuid4(),
            transition_deadline=NOW + timedelta(seconds=30),
            lifecycle_policy_state=None,
            now=NOW,
        )
        == "restarting"
    )


def test_blocked_when_suppressed_without_backoff() -> None:
    assert (
        compute_effective_state(
            pid=None,
            desired_state="running",
            health_running=None,
            health_state=None,
            transition_token=None,
            transition_deadline=None,
            lifecycle_policy_state={"recovery_suppressed_reason": "manual"},
            now=NOW,
        )
        == "blocked"
    )


def test_error_when_health_state_error() -> None:
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=None,
            health_state="error",
            transition_token=None,
            transition_deadline=None,
            lifecycle_policy_state=None,
            now=NOW,
        )
        == "error"
    )


def test_error_when_health_running_false() -> None:
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=False,
            health_state=None,
            transition_token=None,
            transition_deadline=None,
            lifecycle_policy_state=None,
            now=NOW,
        )
        == "error"
    )


def test_starting_running_stopping_stopped() -> None:
    base = {
        "health_running": None,
        "health_state": None,
        "transition_token": None,
        "transition_deadline": None,
        "lifecycle_policy_state": None,
        "now": NOW,
    }
    assert compute_effective_state(pid=None, desired_state="running", **base) == "starting"
    assert compute_effective_state(pid=1, desired_state="running", **base) == "running"
    assert compute_effective_state(pid=1, desired_state="stopped", **base) == "stopping"
    assert compute_effective_state(pid=None, desired_state="stopped", **base) == "stopped"
