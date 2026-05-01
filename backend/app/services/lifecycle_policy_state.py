"""In-memory helpers for the Device.lifecycle_policy_state JSON column.

INVARIANT: ``state`` reads and ``write_state`` writes do NOT lock. Callers
must hold a row-level lock on the Device row (use
``app.services.device_locking.lock_device``) for the entire read-modify-write
window. See app/services/lifecycle_policy.py for canonical usage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect

if TYPE_CHECKING:
    from app.models.appium_node import AppiumNode
    from app.models.device import Device


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().isoformat()


def parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_state() -> dict[str, Any]:
    return {
        "last_failure_source": None,
        "last_failure_reason": None,
        "last_action": None,
        "last_action_at": None,
        "stop_pending": False,
        "stop_pending_reason": None,
        "stop_pending_since": None,
        "recovery_suppressed_reason": None,
        "backoff_until": None,
        "recovery_backoff_attempts": 0,
    }


def state(device: Device) -> dict[str, Any]:
    raw = device.lifecycle_policy_state if isinstance(device.lifecycle_policy_state, dict) else {}
    return {**default_state(), **raw}


def write_state(device: Device, next_state: dict[str, Any]) -> None:
    device_state = sa_inspect(device, raiseerr=False)
    assert device_state is not None and device_state.persistent, (
        "Device must be persistent in a session; callers that write lifecycle_policy_state "
        "must load it through lock_device in the same transaction"
    )

    defaults = default_state()
    device.lifecycle_policy_state = {key: next_state.get(key, default) for key, default in defaults.items()}


def loaded_node(device: Device) -> AppiumNode | None:
    return device.__dict__.get("appium_node")


def set_action(next_state: dict[str, Any], action: str) -> None:
    next_state["last_action"] = action
    next_state["last_action_at"] = now_iso()


def clear_backoff(next_state: dict[str, Any]) -> None:
    next_state["backoff_until"] = None
    next_state["recovery_backoff_attempts"] = 0


def set_backoff(next_state: dict[str, Any], *, base_seconds: int, max_seconds: int) -> str:
    attempts = int(next_state.get("recovery_backoff_attempts") or 0) + 1
    next_state["recovery_backoff_attempts"] = attempts
    seconds = min(max_seconds, base_seconds * (2 ** (attempts - 1)))
    backoff_until = now() + timedelta(seconds=seconds)
    backoff_until_iso = backoff_until.isoformat()
    next_state["backoff_until"] = backoff_until_iso
    return backoff_until_iso
