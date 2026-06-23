"""Pure effective-state derivation for an Appium node."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import uuid

EffectiveNodeStateValue = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "blocked",
    "error",
]


def _is_recovery_blocked(lifecycle_state: dict[str, Any], now: datetime) -> bool:
    suppression_reason = lifecycle_state.get("recovery_suppressed_reason")
    if not (isinstance(suppression_reason, str) and suppression_reason):
        return False
    backoff_raw = lifecycle_state.get("backoff_until")
    backoff_active = False
    if isinstance(backoff_raw, str):
        try:
            backoff_until = datetime.fromisoformat(backoff_raw)
            if backoff_until.tzinfo is None:
                backoff_until = backoff_until.replace(tzinfo=UTC)
            backoff_active = backoff_until > now
        except ValueError:
            backoff_active = False
    return backoff_raw is None or backoff_active


def _desired_state_effective(*, desired_state: str, pid: int | None) -> EffectiveNodeStateValue:
    if desired_state == "running" and pid is None:
        return "starting"
    if desired_state == "stopped" and pid is not None:
        return "stopping"
    if desired_state == "running" and pid is not None:
        return "running"
    return "stopped"


def compute_effective_state(
    *,
    pid: int | None,
    desired_state: str,
    health_running: bool | None,
    health_state: str | None,
    transition_token: uuid.UUID | None,
    transition_deadline: datetime | None,
    lifecycle_policy_state: dict[str, Any] | None,
    now: datetime,
) -> EffectiveNodeStateValue:
    if transition_token is not None and transition_deadline is not None and transition_deadline > now:
        return "restarting"

    lifecycle_state = lifecycle_policy_state or {}
    if _is_recovery_blocked(lifecycle_state, now):
        return "blocked"

    if health_state == "error" or health_running is False:
        return "error"

    return _desired_state_effective(desired_state=desired_state, pid=pid)
