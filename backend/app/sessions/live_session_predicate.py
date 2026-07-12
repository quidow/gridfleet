"""Shared predicate: a Session row is *live* (claims its device).

A "live" session is one that currently holds (or is mid-claim of) its device's
Appium node: ``status IN (running, pending) AND ended_at IS NULL``. ``pending``
is the grid allocate->confirm window — a placeholder row exists before the real
Appium session id is confirmed, so the device is already claimed by the router
and must gate every allocation-class action the same as ``running``.

This contract was hand-copied at ~8 sites (allocation gates, liveness sweep,
run-release, state derivation, fleet capacity); adding ``pending`` to the set
once forced editing every copy. Factoring it here (the ``node_viability.py``
pattern) keeps those sites from drifting — the one site that *missed* the
``pending`` sweep was a confirmed correctness bug (auto-recovery restarting a
node mid-create).

A running probe is a live session for the claim axis but is excluded from the
busy-masking projection via ``masking_live_session_predicate`` (WS-16.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

_LIVE_STATUSES = (SessionStatus.running, SessionStatus.pending)


def live_session_predicate(device_id: uuid.UUID | None = None) -> ColumnElement[bool]:
    """SQL predicate selecting live (running|pending, not-ended) Session rows.

    When *device_id* is supplied the predicate is additionally scoped to that
    device, so callers building a per-device existence check don't repeat the
    ``Session.device_id == ...`` term.
    """
    predicate = Session.status.in_(_LIVE_STATUSES) & Session.ended_at.is_(None)
    if device_id is not None:
        predicate = (Session.device_id == device_id) & predicate
    return predicate


async def device_has_live_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    """Return whether a running or mid-create session claims the device."""
    count = await db.scalar(select(func.count()).select_from(Session).where(live_session_predicate(device_id)))
    return bool(count)


def masking_live_session_predicate(device_id: uuid.UUID | None = None) -> ColumnElement[bool]:
    """Busy-masking variant of :func:`live_session_predicate` — excludes probe rows.

    WS-16.1 (D14): probe rows claim but do not mask. A live probe session gates
    allocation-class actions through the claim predicate above, but must not
    derive ``busy`` — otherwise every device's ledger flips
    available→busy→available per probe cadence, emitting operational-state
    edges with no operator meaning. This is the ONE place the exclusion is
    spelled; the projection (``app.devices.services.state``) and its capacity
    twin consume it from here.
    """
    return live_session_predicate(device_id) & or_(Session.test_name.is_(None), Session.test_name != PROBE_TEST_NAME)


async def device_has_masking_live_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    """Return whether a non-probe running or mid-create session claims the device."""
    count = await db.scalar(select(func.count()).select_from(Session).where(masking_live_session_predicate(device_id)))
    return bool(count)
