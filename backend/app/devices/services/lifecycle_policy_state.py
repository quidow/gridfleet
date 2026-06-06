"""In-memory helpers for the Device.lifecycle_policy_state JSON column.

INVARIANT: ``state`` reads and ``write_state`` writes do NOT lock. Callers
must hold a row-level lock on the Device row (use
``app.devices.locking.lock_device``) for the entire read-modify-write
window. See app/services/lifecycle_policy.py for canonical usage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect

if TYPE_CHECKING:
    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device


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
        "maintenance_reason": None,
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


def set_deferred_stop(next_state: dict[str, Any], *, reason: str) -> None:
    """Mark the deferred-auto-stop intent on the working state dict.

    The caller is still responsible for ``write_state`` and for emitting any
    ``lifecycle_deferred_stop`` incident under the device row lock.
    """
    next_state["stop_pending"] = True
    next_state["stop_pending_reason"] = reason
    next_state["stop_pending_since"] = now_iso()
    set_action(next_state, "auto_stop_deferred")


def clear_deferred_stop(next_state: dict[str, Any]) -> None:
    """Clear the deferred-auto-stop intent on the working state dict.

    Does NOT stamp ``last_action`` — callers that want a specific trail entry
    (e.g. ``auto_stop_cleared``) call ``set_action`` themselves.
    """
    next_state["stop_pending"] = False
    next_state["stop_pending_reason"] = None
    next_state["stop_pending_since"] = None


def record_recovery_started(next_state: dict[str, Any]) -> None:
    next_state["recovery_suppressed_reason"] = None
    set_action(next_state, "recovery_started")


def record_recovery_failed(
    next_state: dict[str, Any],
    *,
    source: str,
    reason: str,
    suppression_reason: str,
) -> None:
    next_state["last_failure_source"] = source
    next_state["last_failure_reason"] = reason
    next_state["recovery_suppressed_reason"] = suppression_reason
    set_action(next_state, "recovery_failed")


def record_backoff_suppressed(next_state: dict[str, Any], *, until_iso: str) -> None:
    next_state["recovery_suppressed_reason"] = f"Backing off until {until_iso}"
    set_action(next_state, "recovery_suppressed")


def record_recovery_recovered(next_state: dict[str, Any]) -> None:
    clear_backoff(next_state)
    next_state["recovery_suppressed_reason"] = None
    set_action(next_state, "auto_recovered")


MAINTENANCE_HOLD_SUPPRESSION_REASON = "Device is in maintenance mode"

# Recorded by ``attempt_auto_recovery`` when blocked by an active client
# session. Unlike other suppression reasons (maintenance, cooldown, etc.)
# this one is transient by definition — the moment the session ends, the
# blocker is gone. Held in a constant so ``handle_session_finished`` can
# clear it without re-stating the literal.
CLIENT_SESSION_RUNNING_SUPPRESSION_REASON = "A client session is still running"


def clear_maintenance_recovery_suppression(device: Device) -> None:
    """Clear lifecycle suppression that ``handle_health_failure`` records when a
    device fails a probe while held in maintenance.

    Only clears the maintenance-tautology reason
    (``MAINTENANCE_HOLD_SUPPRESSION_REASON``). Other suppressions
    (``"Node restart failed"``, ``"Recovery probe failed"``, an active
    backoff window, etc.) describe a real condition that is independent of
    the maintenance hold and must survive an operator-driven exit. No-op
    when those are present.

    Caller must hold the device row lock and is responsible for the commit;
    this helper performs an in-memory read-modify-write through ``write_state``
    and does not lock or touch the database directly.
    """
    next_state = state(device)
    if next_state.get("recovery_suppressed_reason") != MAINTENANCE_HOLD_SUPPRESSION_REASON:
        return
    clear_backoff(next_state)
    next_state["recovery_suppressed_reason"] = None
    set_action(next_state, "maintenance_exited")
    write_state(device, next_state)


def clear_operator_start_suppression(device: Device) -> None:
    """Clear recovery-suppression residue when an operator explicitly starts the node.

    An operator stop records ``recovery_suppressed_reason`` (e.g. "Operator stopped
    the node") plus a sticky RECOVERY-axis deny intent. The start path revokes the
    intents but leaves the JSON suppression in place, so the device keeps deriving
    ``recovery_state="suppressed"`` (presenter "blocked" / "Recovery Paused") even
    though it is running and available. An explicit operator start overrides any
    prior failure/suppression, so clear the suppression reason, the backoff window,
    and the stale failure trail, then stamp a coherent action.

    Leaves the maintenance-hold tautology
    (``MAINTENANCE_HOLD_SUPPRESSION_REASON``) untouched: a node cannot be started
    while the device is held in maintenance, and that suppression is governed by
    the maintenance-exit path instead. No-op when nothing is suppressed so an
    already-clean device emits no spurious action churn.

    Caller must hold the device row lock and is responsible for the commit; this
    helper performs an in-memory read-modify-write through ``write_state``.
    """
    next_state = state(device)
    suppression = next_state.get("recovery_suppressed_reason")
    if suppression == MAINTENANCE_HOLD_SUPPRESSION_REASON:
        return
    backoff_active = bool(next_state.get("backoff_until")) or bool(next_state.get("recovery_backoff_attempts"))
    if not suppression and not backoff_active and not next_state.get("last_failure_reason"):
        return
    clear_backoff(next_state)
    next_state["recovery_suppressed_reason"] = None
    next_state["last_failure_source"] = None
    next_state["last_failure_reason"] = None
    set_action(next_state, "operator_started")
    write_state(device, next_state)


def clear_self_heal_suppression(device: Device) -> bool:
    """Clear recovery-suppression residue when a device self-heals naturally.

    A device can recover without any recovery path firing: an agent restart →
    reconvergence leaves the node running, the device available, and the health
    checks green, but neither ``attempt_auto_recovery`` (which short-circuits
    when the node is already running) nor an operator start ran. The
    ``recovery_suppressed_reason`` recorded by the last failed recovery attempt
    (e.g. "Recovery probe failed") therefore lingers on the JSON forever, so the
    device keeps deriving ``recovery_state="suppressed"`` (presenter "Recovery
    Paused" → ``needs_attention=true``) despite being healthy.

    Clears the suppression reason, the backoff window, and the stale failure
    trail, then stamps ``self_healed``. Returns True when residue was actually
    cleared so callers can record the self-heal exactly once.

    Leaves the maintenance-hold tautology
    (``MAINTENANCE_HOLD_SUPPRESSION_REASON``) untouched — a device held in
    maintenance is governed by the maintenance-exit path, not connectivity
    self-heal. No-op (returns False) when nothing is suppressed so a clean
    device emits no action churn on every connectivity cycle.

    Caller must hold the device row lock, must gate on ``device.recovery_allowed``
    (an active operator-stop deny intent makes the suppression legitimate and
    sticky by design), and is responsible for the commit; this helper performs an
    in-memory read-modify-write through ``write_state``.
    """
    next_state = state(device)
    suppression = next_state.get("recovery_suppressed_reason")
    if suppression == MAINTENANCE_HOLD_SUPPRESSION_REASON:
        return False
    has_residue = (
        bool(suppression)
        or next_state.get("last_action") == "recovery_suppressed"
        or bool(next_state.get("backoff_until"))
        or bool(next_state.get("recovery_backoff_attempts"))
    )
    if not has_residue:
        return False
    clear_backoff(next_state)
    next_state["recovery_suppressed_reason"] = None
    next_state["last_failure_source"] = None
    next_state["last_failure_reason"] = None
    set_action(next_state, "self_healed")
    write_state(device, next_state)
    return True


def set_maintenance_reason(device: Device, reason: str) -> None:
    next_state = state(device)
    next_state["maintenance_reason"] = reason
    write_state(device, next_state)


def clear_maintenance_reason(device: Device) -> None:
    next_state = state(device)
    next_state["maintenance_reason"] = None
    write_state(device, next_state)


def set_backoff(next_state: dict[str, Any], *, base_seconds: int, max_seconds: int) -> str:
    attempts = int(next_state.get("recovery_backoff_attempts") or 0) + 1
    next_state["recovery_backoff_attempts"] = attempts
    seconds = min(max_seconds, base_seconds * (2 ** (attempts - 1)))
    backoff_until = now() + timedelta(seconds=seconds)
    backoff_until_iso = backoff_until.isoformat()
    next_state["backoff_until"] = backoff_until_iso
    return backoff_until_iso
