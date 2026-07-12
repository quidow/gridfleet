"""In-memory helpers for the Device.lifecycle_policy_state JSON column.

INVARIANT: ``state`` reads and ``write_state`` writes do NOT lock. Callers
must hold a row-level lock on the Device row (use
``app.devices.locking.lock_device``) for the entire read-modify-write
window. See app/services/lifecycle_policy.py for canonical usage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect

from app.core import timeutil

if TYPE_CHECKING:
    from datetime import datetime

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device


# ``now``/``parse_iso`` delegate to the shared app.core.timeutil (Q10); kept as named
# wrappers here so the existing importers (policy.py, lifecycle_policy_summary.py) do not
# change and mypy sees an explicit definition.
def now() -> datetime:
    return timeutil.now_utc()


def now_iso() -> str:
    return now().isoformat()


def parse_iso(raw: object) -> datetime | None:
    return timeutil.parse_iso(raw)


def default_state() -> dict[str, Any]:
    return {
        "deferred_stop": False,
        "deferred_stop_reason": None,
        "deferred_stop_since": None,
        "maintenance_reason": None,
    }


def state(device: Device) -> dict[str, Any]:
    raw = device.lifecycle_policy_state if isinstance(device.lifecycle_policy_state, dict) else {}
    return {**default_state(), **raw}


def in_maintenance(device: Device) -> bool:
    """True when a maintenance reason is recorded in the device's lifecycle policy state."""
    return state(device).get("maintenance_reason") is not None


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


def set_deferred_stop(next_state: dict[str, Any], *, reason: str) -> None:
    """Mark the deferred-auto-stop intent on the working state dict.

    The caller is still responsible for ``write_state`` and for emitting any
    ``lifecycle_deferred_stop`` incident under the device row lock.
    """
    next_state["deferred_stop"] = True
    next_state["deferred_stop_reason"] = reason
    next_state["deferred_stop_since"] = now_iso()


def clear_deferred_stop(next_state: dict[str, Any]) -> None:
    """Clear the deferred-auto-stop intent on the working state dict.

    Does NOT stamp remediation-log action rows; callers that want a specific
    trail entry append it through ``app.lifecycle.services.remediation_log``.
    """
    next_state["deferred_stop"] = False
    next_state["deferred_stop_reason"] = None
    next_state["deferred_stop_since"] = None


MAINTENANCE_HOLD_SUPPRESSION_REASON = "Device is in maintenance mode"

# Recorded by ``attempt_auto_recovery`` when blocked by an active client
# session. Unlike other suppression reasons (maintenance, cooldown, etc.)
# this one is transient by definition — the moment the session ends, the
# blocker is gone. Held in a constant so ``handle_session_finished`` can
# clear it without re-stating the literal.
CLIENT_SESSION_RUNNING_SUPPRESSION_REASON = "A client session is still running"


def set_maintenance_reason(device: Device, reason: str) -> None:
    next_state = state(device)
    next_state["maintenance_reason"] = reason
    write_state(device, next_state)


def clear_maintenance_reason(device: Device) -> None:
    next_state = state(device)
    next_state["maintenance_reason"] = None
    write_state(device, next_state)
