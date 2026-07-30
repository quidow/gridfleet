"""Pure-function tests for Appium node effective-state derivation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args

from app.appium_nodes.services.effective_state import EffectiveNodeStateValue, compute_effective_state
from app.core.timeutil import now_utc
from app.devices.schemas.device import EffectiveNodeState

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
            now=NOW,
        )
        == "running"
    )


def test_blocked_rung_is_gone() -> None:
    """Backoff/review no longer mask the real process state (drift review 1.1/1.5).

    ``EffectiveNodeStateValue`` is the computation vocabulary; ``EffectiveNodeState``
    (``app.devices.schemas.device``) is the hand-maintained public OpenAPI copy the
    frontend derives from. This pins that the synthetic ``blocked`` state stays
    removed from both, and that the two vocabularies stay in lockstep — a schema
    copy that drifts back to including ``"blocked"`` would type-check clean but
    slip past a check that only looked at the service literal. ``restarting`` is
    the only synthetic rung left.
    """
    assert "blocked" not in get_args(EffectiveNodeStateValue)
    assert set(get_args(EffectiveNodeState)) == set(get_args(EffectiveNodeStateValue))
    now = now_utc()
    state = compute_effective_state(
        pid=1234,
        desired_state="running",
        health_running=True,
        health_state="ok",
        restart_requested_at=None,
        started_at=now - timedelta(minutes=5),
        restart_window_sec=90,
        now=now,
    )
    assert state == "running"


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
        "now": NOW,
    }
    assert compute_effective_state(pid=None, desired_state="running", **base) == "starting"
    assert compute_effective_state(pid=1, desired_state="running", **base) == "running"
    assert compute_effective_state(pid=1, desired_state="stopped", **base) == "stopping"
    assert compute_effective_state(pid=None, desired_state="stopped", **base) == "stopped"
