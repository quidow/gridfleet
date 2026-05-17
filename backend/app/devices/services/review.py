"""Helpers for the ``Device.review_required`` shelving flag.

``review_required`` is set by ``attempt_auto_recovery`` after consecutive
recovery probe failures cross a settings-driven threshold. Once set, the
device is removed from automated recovery scope. Sanctioned operator
actions clear it: exit-maintenance, restore-from-run, re-verify, and
restart-node.

Keeping the set/clear surface narrow here (a single set and a single clear
helper) avoids drift between callers and gives the audit hook one source
of truth for the event payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.devices.models import DeviceEventType
from app.devices.services.event import record_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


async def mark_review_required(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
    source: str,
) -> bool:
    """Flag ``device`` as needing operator review. Idempotent — returns False
    if already flagged."""
    if not hasattr(device, "review_required"):
        return False
    if device.review_required:
        if device.review_reason != reason:
            device.review_reason = reason
        return False
    device.review_required = True
    device.review_reason = reason
    device.review_set_at = datetime.now(UTC)
    await record_event(
        db,
        device.id,
        DeviceEventType.lifecycle_recovery_suppressed,
        {
            "review_required": True,
            "review_reason": reason,
            "source": source,
            "review_set_at": device.review_set_at.isoformat(),
        },
    )
    return True


async def clear_review_required(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
    source: str,
) -> bool:
    """Clear the review flag and record the audit event. Idempotent — returns
    False when the flag was already off."""
    if not getattr(device, "review_required", False):
        return False
    previous_reason = device.review_reason
    device.review_required = False
    device.review_reason = None
    device.review_set_at = None
    await record_event(
        db,
        device.id,
        DeviceEventType.lifecycle_recovered,
        {
            "review_required": False,
            "previous_reason": previous_reason,
            "source": source,
            "cleared_reason": reason,
        },
    )
    return True
