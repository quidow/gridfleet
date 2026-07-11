"""Read-time projection: why automated recovery can('t) act on a device NOW.

One ladder consulted by both the write path (attempt_auto_recovery) and every
read path (lifecycle_policy_summary badge, node effective-state). Replaces the
stored shadows — Device.recovery_allowed/recovery_blocked_reason and the
lifecycle_policy_state recovery_suppressed_reason key — which needed three GC
helpers and an age-gate (S10) purely because a stored copy can go stale.

Kind precedence mirrors the retired attempt_auto_recovery gate order:
review > recovery-deny (operator/maintenance/cooldown, via decide_recovery) >
not_ready > deferred_stop > session > backoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.timeutil import now_utc
from app.devices.models import DeviceIntent
from app.devices.services.decision import decide_recovery, parse_command
from app.devices.services.intent_reconciler import gather_decision_facts
from app.devices.services.lifecycle_policy_state import (
    CLIENT_SESSION_RUNNING_SUPPRESSION_REASON,
)
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.readiness import is_ready_for_use_async
from app.lifecycle.services.escalation import backoff_active
from app.sessions.live_session_predicate import device_has_live_session

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


class RecoveryBlockKind(StrEnum):
    review = "review"
    operator = "operator"
    maintenance = "maintenance"
    cooldown = "cooldown"
    not_ready = "not_ready"
    deferred_stop = "deferred_stop"
    session = "session"
    backoff = "backoff"


SUPPRESSED_KINDS = frozenset(
    {
        RecoveryBlockKind.review,
        RecoveryBlockKind.operator,
        RecoveryBlockKind.maintenance,
        RecoveryBlockKind.cooldown,
        RecoveryBlockKind.session,
    }
)


@dataclass(frozen=True)
class RecoveryAvailability:
    allowed: bool
    reason: str | None
    kind: RecoveryBlockKind | None


_ALLOWED = RecoveryAvailability(allowed=True, reason=None, kind=None)


async def recovery_availability(  # noqa: PLR0911 - the guard ladder is one return per rung
    db: AsyncSession, device: Device, *, now: datetime | None = None, ready: bool | None = None
) -> RecoveryAvailability:
    # ponytail: consulted per-device in the device-list serializer (serialize_device),
    # so a list poll runs its handful of indexed queries once per row — the same N+1
    # shape the surrounding serialize already has (reservation, assert_runnable). The
    # list path passes ``ready`` from its batched readiness so the pack catalog is not
    # reloaded per row; the remaining per-row queries are indexed and cheap at lab scale.
    now = now or now_utc()
    if device.review_required:
        return RecoveryAvailability(
            False,
            device.review_reason or "Device shelved — operator review required",
            RecoveryBlockKind.review,
        )

    stored = (await db.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    commands = [c for c in (parse_command(row, now) for row in stored) if c is not None]
    facts = await gather_decision_facts(db, device, now)
    decision = decide_recovery(commands, facts)
    if not decision.allowed:
        return RecoveryAvailability(
            False,
            decision.reason or "Recovery is blocked by orchestration intent",
            _deny_kind(decision.source),
        )

    if ready is None:
        ready = await is_ready_for_use_async(db, device)
    if not ready:
        return RecoveryAvailability(False, "Device setup or verification is incomplete", RecoveryBlockKind.not_ready)

    state = policy_state(device)
    if state.get("deferred_stop"):
        return RecoveryAvailability(
            False, "Waiting for active client session to finish", RecoveryBlockKind.deferred_stop
        )

    if await device_has_live_session(db, device.id):
        return RecoveryAvailability(False, CLIENT_SESSION_RUNNING_SUPPRESSION_REASON, RecoveryBlockKind.session)

    deadline = backoff_active(state)
    if deadline is not None:
        return RecoveryAvailability(False, f"Backing off until {deadline.isoformat()}", RecoveryBlockKind.backoff)

    return _ALLOWED


def _deny_kind(source: str | None) -> RecoveryBlockKind:
    if source == "maintenance":
        return RecoveryBlockKind.maintenance
    if source == "cooldown":
        return RecoveryBlockKind.cooldown
    return RecoveryBlockKind.operator
