"""One home for "who holds this device right now" — the claim vocabulary.

Three claim axes gate device actions:

- **session** — a live ``Session`` row (``running|pending``, not ended). The
  definition is owned by ``app.sessions.live_session_predicate`` (its docstring
  records the pending-omission bug that motivated the chokepoint) and is
  re-exported here so this module presents the complete vocabulary.
  A live probe row claims the device but is excluded from the busy-masking
  projection (masking_* variants, WS-16.1).
- **reservation** — an active ``DeviceReservation`` row: ``released_at IS NULL``.
- **verification** — an unexpired verification ``DeviceIntent`` lease. A lease
  carrying a terminal ``outcome`` stamp is a tombstone awaiting deletion, not
  an active claim (WS-15.3).

Each axis's "active" definition appears in SQL exactly once, in (or re-exported
by) this module. Hand-recomposing an axis at a call site is the drift class the
contract test ``tests/contracts/test_claim_predicates.py`` guards against.
Owner modules still *write* their rows (run release stamps ``released_at``,
intent GC deletes on ``expires_at``); this module owns read-side gating only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, or_, select

from app.devices.models import Device
from app.devices.models.intent import DeviceIntent
from app.devices.models.reservation import DeviceReservation
from app.devices.services.intent_types import VERIFICATION_OUTCOME_KEY, CommandKind, verification_intent_source
from app.sessions.live_session_predicate import (
    device_has_live_session,
    device_has_masking_live_session,
    live_session_predicate,
    masking_live_session_predicate,
)
from app.sessions.models import Session

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.expression import ColumnElement

__all__ = [
    "active_reservation_exists",
    "device_has_live_session",
    "device_has_masking_live_session",
    "device_has_verification_lease",
    "device_is_reserved",
    "is_verification_lease_active",
    "live_session_exists",
    "live_session_predicate",
    "masking_live_session_exists",
    "masking_live_session_predicate",
    "reservation_active",
    "verification_lease_exists",
    "verification_lease_predicate",
]


def live_session_exists() -> ColumnElement[bool]:
    """Correlated EXISTS for ``Device`` selects: a live session claims this device."""
    return exists(select(Session.id).where(Session.device_id == Device.id, live_session_predicate()))


def masking_live_session_exists() -> ColumnElement[bool]:
    """Correlated EXISTS for ``Device`` selects: busy-masking live sessions only.

    Probe rows claim (``live_session_exists``) but do not mask (WS-16.1); the
    operational-state projection's ``busy`` legs read this variant.
    """
    return exists(select(Session.id).where(Session.device_id == Device.id, masking_live_session_predicate()))


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


def verification_lease_exists(*, now: datetime) -> ColumnElement[bool]:
    """Correlated EXISTS for ``Device`` selects: an unexpired verification lease."""
    return exists(
        select(DeviceIntent.id).where(
            DeviceIntent.device_id == Device.id,
            DeviceIntent.kind == CommandKind.verification_start,
            DeviceIntent.payload[VERIFICATION_OUTCOME_KEY].astext.is_(None),
            or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now),
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
    return cast(
        "ColumnElement[bool]",
        (DeviceIntent.device_id == device_id)
        & (DeviceIntent.source == verification_intent_source(device_id))
        & DeviceIntent.payload[VERIFICATION_OUTCOME_KEY].astext.is_(None)
        & or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now),
    )


async def device_has_verification_lease(db: AsyncSession, device_id: UUID, *, now: datetime) -> bool:
    """True iff an unexpired verification lease claims the device."""
    return (
        await db.execute(select(DeviceIntent.id).where(verification_lease_predicate(device_id, now=now)).limit(1))
    ).first() is not None


def is_verification_lease_active(
    *,
    source: str,
    payload: Mapping[str, Any],
    expires_at: datetime | None,
    device_id: UUID,
    now: datetime,
) -> bool:
    """In-memory twin of ``verification_lease_predicate`` for callers that have
    already loaded the intent row (e.g. the device decision snapshot) and must
    not re-issue the SQL predicate. Keeps the lease definition single-homed."""
    return (
        source == verification_intent_source(device_id)
        and payload.get(VERIFICATION_OUTCOME_KEY) is None
        and (expires_at is None or expires_at > now)
    )
