"""Single owner of the shared remediation-escalation ladder.

The ladder's memory is the append-only ``device_remediation_log`` table.
Every automated remediation -- recovery probe, node-health restart, appium
start retry -- shares one derived attempt count and backoff window. The
backoff saturates at ``general.lifecycle_recovery_backoff_max_sec``; attempts
continue indefinitely — there is no terminal rung.

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
    from app.devices.locking import LockedDevice
    from app.lifecycle.services.remediation_log import LadderState


@dataclass(frozen=True)
class EscalationOutcome:
    backoff_until_iso: str
    attempts: int
    ladder: LadderState


async def escalate_remediation_failure(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    settings: SettingsReader,
    source: str,
    reason: str,
    prior: LadderState | None = None,
) -> EscalationOutcome:
    """Record one failed automated remediation as an append-only attempt row."""
    entry, ladder = await remediation_log.append_attempt(
        db,
        locked,
        source=source,
        reason=reason,
        settings=settings,
        prior=prior,
    )
    assert entry.backoff_until is not None
    return EscalationOutcome(
        backoff_until_iso=entry.backoff_until.isoformat(),
        attempts=ladder.attempts,
        ladder=ladder,
    )
