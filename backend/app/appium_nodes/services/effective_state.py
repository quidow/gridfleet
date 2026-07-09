"""Pure effective-state derivation for an Appium node."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

EffectiveNodeStateValue = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "blocked",
    "error",
]


def _backoff_active(lifecycle_state: dict[str, Any], now: datetime) -> bool:
    backoff_raw = lifecycle_state.get("backoff_until")
    if not isinstance(backoff_raw, str):
        return False
    try:
        backoff_until = datetime.fromisoformat(backoff_raw)
    except ValueError:
        return False
    if backoff_until.tzinfo is None:
        backoff_until = backoff_until.replace(tzinfo=UTC)
    return backoff_until > now


def _desired_state_effective(*, desired_state: str, pid: int | None) -> EffectiveNodeStateValue:
    if desired_state == "running" and pid is None:
        return "starting"
    if desired_state == "stopped" and pid is not None:
        return "stopping"
    if desired_state == "running" and pid is not None:
        return "running"
    return "stopped"


def compute_effective_state(  # noqa: PLR0913 - keyword-only node observation fields folded into one verdict
    *,
    pid: int | None,
    desired_state: str,
    health_running: bool | None,
    health_state: str | None,
    restart_requested_at: datetime | None,
    started_at: datetime | None,
    restart_window_sec: int,
    lifecycle_policy_state: dict[str, Any] | None,
    review_required: bool,
    now: datetime,
) -> EffectiveNodeStateValue:
    if (
        restart_requested_at is not None
        and restart_requested_at > now - timedelta(seconds=restart_window_sec)
        and (started_at is None or started_at < restart_requested_at)
    ):
        # Read-time bounding replaces the lease-expiry sweep; a dead agent can
        # pin "restarting" for at most restart_window_sec.
        return "restarting"

    if review_required or _backoff_active(lifecycle_policy_state or {}, now):
        return "blocked"

    if health_state == "error" or health_running is False:
        return "error"

    return _desired_state_effective(desired_state=desired_state, pid=pid)
