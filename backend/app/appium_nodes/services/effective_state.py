"""Pure effective-state derivation for an Appium node."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

EffectiveNodeStateValue = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "error",
]


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
    restart_requested_at: datetime | None,
    started_at: datetime | None,
    restart_window_sec: int,
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

    if health_state == "error" or health_running is False:
        return "error"

    return _desired_state_effective(desired_state=desired_state, pid=pid)
