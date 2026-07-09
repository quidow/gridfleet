"""Pure-function tests for Appium node effective-state derivation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.appium_nodes.services.effective_state import compute_effective_state

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def test_pending_watermark_is_restarting_within_window() -> None:
    watermark = NOW - timedelta(seconds=30)
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=True,
            health_state=None,
            restart_requested_at=watermark,
            started_at=watermark - timedelta(seconds=600),
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=False,
            now=NOW,
        )
        == "restarting"
    )


def test_satisfied_watermark_is_running() -> None:
    watermark = NOW - timedelta(seconds=30)
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=True,
            health_state=None,
            restart_requested_at=watermark,
            started_at=NOW - timedelta(seconds=5),
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=False,
            now=NOW,
        )
        == "running"
    )


def test_expired_watermark_self_clears_at_read_time() -> None:
    watermark = NOW - timedelta(seconds=600)
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=True,
            health_state=None,
            restart_requested_at=watermark,
            started_at=watermark - timedelta(seconds=600),
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=False,
            now=NOW,
        )
        == "running"
    )


def test_blocked_when_review_required() -> None:
    assert (
        compute_effective_state(
            pid=None,
            desired_state="running",
            health_running=None,
            health_state=None,
            restart_requested_at=None,
            started_at=None,
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=True,
            now=NOW,
        )
        == "blocked"
    )


def test_blocked_when_backoff_active() -> None:
    assert (
        compute_effective_state(
            pid=None,
            desired_state="running",
            health_running=None,
            health_state=None,
            restart_requested_at=None,
            started_at=None,
            restart_window_sec=120,
            lifecycle_policy_state={"backoff_until": (NOW + timedelta(seconds=120)).isoformat()},
            review_required=False,
            now=NOW,
        )
        == "blocked"
    )


def test_not_blocked_when_only_suppression() -> None:
    """Stored suppression alone no longer pins blocked (behavior change #4)."""
    assert (
        compute_effective_state(
            pid=None,
            desired_state="running",
            health_running=None,
            health_state=None,
            restart_requested_at=None,
            started_at=None,
            restart_window_sec=120,
            lifecycle_policy_state={"recovery_suppressed_reason": "manual"},
            review_required=False,
            now=NOW,
        )
        == "starting"
    )


def test_error_when_health_state_error() -> None:
    assert (
        compute_effective_state(
            pid=123,
            desired_state="running",
            health_running=None,
            health_state="error",
            restart_requested_at=None,
            started_at=None,
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=False,
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
            restart_requested_at=None,
            started_at=None,
            restart_window_sec=120,
            lifecycle_policy_state=None,
            review_required=False,
            now=NOW,
        )
        == "error"
    )


def test_starting_running_stopping_stopped() -> None:
    base = {
        "health_running": None,
        "health_state": None,
        "restart_requested_at": None,
        "started_at": None,
        "restart_window_sec": 120,
        "lifecycle_policy_state": None,
        "review_required": False,
        "now": NOW,
    }
    assert compute_effective_state(pid=None, desired_state="running", **base) == "starting"
    assert compute_effective_state(pid=1, desired_state="running", **base) == "running"
    assert compute_effective_state(pid=1, desired_state="stopped", **base) == "stopping"
    assert compute_effective_state(pid=None, desired_state="stopped", **base) == "stopped"
