"""One home for "who holds this device right now" — the claim vocabulary.

Three claim axes gate device actions:

- **session** — a live ``Session`` row (``running|pending``, not ended). The
  definition is owned by ``app.sessions.live_session_predicate`` (its docstring
  records the pending-omission bug that motivated the chokepoint) and is
  re-exported here so this module presents the complete vocabulary.
- **reservation** — an active ``DeviceReservation`` row: ``released_at IS NULL``.
- **verification** — an unexpired verification ``DeviceIntent`` lease.

Each axis's "active" definition appears in SQL exactly once, in (or re-exported
by) this module. Hand-recomposing an axis at a call site is the drift class the
contract test ``tests/contracts/test_claim_predicates.py`` guards against.
Owner modules still *write* their rows (run release stamps ``released_at``,
intent GC deletes on ``expires_at``); this module owns read-side gating only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, or_, select

from app.devices.models import Device
from app.devices.models.intent import DeviceIntent
from app.devices.models.reservation import DeviceReservation
from app.devices.services.intent_types import verification_intent_source
from app.sessions.live_session_predicate import device_has_live_session, live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.expression import ColumnElement

__all__ = [
    "active_reservation_exists",
    "device_has_live_session",
    "device_has_verification_lease",
    "device_is_reserved",
    "live_session_exists",
    "live_session_predicate",
    "reservation_active",
    "verification_lease_predicate",
]


def live_session_exists() -> ColumnElement[bool]:
    """Correlated EXISTS for ``Device`` selects: a live session claims this device."""
    return exists(select(Session.id).where(Session.device_id == Device.id, live_session_predicate()))


def reservation_active() -> ColumnElement[bool]:
    """Row-level clause: this ``DeviceReservation`` row is an active hold."""
    return DeviceReservation.released_at.is_(None)


def active_reservation_exists() -> ColumnElement[bool]:
    """Correlated EXISTS clause: an active reservation for the current ``Device`` row.

    Use in ``select(...).where(~active_reservation_exists())`` or as a labeled
    column ``active_reservation_exists().label("is_reserved")``.
    """
    return exists(
        select(DeviceReservation.id).where(
            DeviceReservation.device_id == Device.id,
            reservation_active(),
        )
    )


async def device_is_reserved(db: AsyncSession, device_id: UUID) -> bool:
    """True iff the device has an active reservation row."""
    return (
        await db.execute(
            select(DeviceReservation.id).where(DeviceReservation.device_id == device_id, reservation_active()).limit(1)
        )
    ).first() is not None


def verification_lease_predicate(device_id: UUID, *, now: datetime) -> ColumnElement[bool]:
    """SQL predicate: an unexpired verification lease intent for *device_id*."""
    return (
        (DeviceIntent.device_id == device_id)
        & (DeviceIntent.source == verification_intent_source(device_id))
        & or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now)
    )


async def device_has_verification_lease(db: AsyncSession, device_id: UUID, *, now: datetime) -> bool:
    """True iff an unexpired verification lease claims the device."""
    return (
        await db.execute(select(DeviceIntent.id).where(verification_lease_predicate(device_id, now=now)).limit(1))
    ).first() is not None
