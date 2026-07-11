"""Single owner of the shared remediation-escalation ladder.

Every automated remediation -- recovery probe, node-health restart, appium
start retry -- shares ONE ladder on ``Device.lifecycle_policy_state``:
``recovery_backoff_attempts`` (consecutive failed remediations) and
``backoff_until`` (exponential wait before the next attempt), promoted to
``Device.review_required`` once attempts cross
``general.lifecycle_recovery_review_threshold``.

Detection debounce (ip_ping duration windows, ``general.node_fail_window_sec``,
probe-unanswered counting, the link-repair attempt budget) stays with each
observer; this module owns only what happens AFTER a remediation fails.

Callers hold the device row lock across the read-modify-write and stay
responsible for ``write_state`` + commit. This module never calls
``write_state`` (see tests/lifecycle/test_lifecycle_write_state_allowlist.py);
callers outside the allowlist go through
``app.lifecycle.services.actions.escalate_device_remediation_failure``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.devices.services.lifecycle_policy_state import now, parse_iso, set_backoff

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.devices.protocols import ReviewProtocol


@dataclass(frozen=True)
class EscalationOutcome:
    backoff_until_iso: str
    attempts: int
    shelved: bool


def backoff_active(state: dict[str, Any]) -> datetime | None:
    """Return the active shared backoff deadline, or None when remediation may proceed."""
    deadline = parse_iso(state.get("backoff_until"))
    if deadline is not None and deadline > now():
        return deadline
    return None


async def escalate_remediation_failure(
    db: AsyncSession,
    device: Device,
    state: dict[str, Any],
    *,
    settings: SettingsReader,
    review: ReviewProtocol,
    source: str,
    reason: str,
) -> EscalationOutcome:
    """Record one failed automated remediation on the working ``state`` dict.

    Increments the shared attempt counter, arms the exponential backoff
    window, and promotes the device to ``review_required`` once attempts
    reach the shared threshold. Mutates ``state`` in place; the caller owns
    ``write_state`` and the commit, under the device row lock.
    """
    base_seconds = settings.get_int("general.lifecycle_recovery_backoff_base_sec")
    max_seconds = max(base_seconds, settings.get_int("general.lifecycle_recovery_backoff_max_sec"))
    backoff_until_iso = set_backoff(state, base_seconds=base_seconds, max_seconds=max_seconds)
    attempts = int(state.get("recovery_backoff_attempts") or 0)
    shelved = attempts >= settings.get_int("general.lifecycle_recovery_review_threshold")
    if shelved:
        await review.mark_review_required(db, device, reason=reason, source=source)
    return EscalationOutcome(backoff_until_iso=backoff_until_iso, attempts=attempts, shelved=shelved)
