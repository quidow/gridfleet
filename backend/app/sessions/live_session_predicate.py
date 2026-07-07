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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.sessions.models import Session, SessionStatus

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
