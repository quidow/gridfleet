"""Single owner of the shared remediation-escalation ladder.

The ladder's memory is the append-only ``device_remediation_log`` table.
Every automated remediation -- recovery probe, node-health restart, appium
start retry -- shares one derived attempt count and backoff window, promoted
to ``Device.review_required`` once attempts cross
``general.lifecycle_recovery_review_threshold``.

Detection debounce (ip_ping duration windows, ``general.node_fail_window_sec``,
probe-unanswered counting, the link-repair attempt budget) stays with each
observer; this module owns only what happens AFTER a remediation fails.

This module never calls ``write_state`` (see
tests/lifecycle/test_lifecycle_write_state_allowlist.py); callers outside the
allowlist go through
``app.lifecycle.services.actions.escalate_device_remediation_failure``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.lifecycle.services import remediation_log

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.devices.protocols import ReviewProtocol
    from app.lifecycle.services.remediation_log import LadderState


@dataclass(frozen=True)
class EscalationOutcome:
    backoff_until_iso: str
    attempts: int
    shelved: bool
    ladder: LadderState


async def escalate_remediation_failure(
    db: AsyncSession,
    device: Device,
    *,
    settings: SettingsReader,
    review: ReviewProtocol,
    source: str,
    reason: str,
    prior: LadderState | None = None,
) -> EscalationOutcome:
    """Record one failed automated remediation as an append-only attempt row."""
    entry, ladder = await remediation_log.append_attempt(
        db,
        device.id,
        source=source,
        reason=reason,
        settings=settings,
        prior=prior,
    )
    shelved = ladder.attempts >= settings.get_int("general.lifecycle_recovery_review_threshold")
    if shelved:
        await review.mark_review_required(db, device, reason=reason, source=source)
    assert entry.backoff_until is not None
    return EscalationOutcome(
        backoff_until_iso=entry.backoff_until.isoformat(),
        attempts=ladder.attempts,
        shelved=shelved,
        ladder=ladder,
    )
