"""Operator-initiated session kill (sessions page rework spec).

Best-effort DELETE of the live Appium session, then terminalize the DB row via
the existing ``update_session_status`` path so intent revoke + device lifecycle
run exactly like a natural session end. The row is ALWAYS terminalized: when
the Appium DELETE fails (or no target resolves) the row still leaves the live
set, which is precisely the condition under which the ``session_sync`` orphan
sweep kills any still-alive Appium session on the next tick — no zombie path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.grid import appium_direct
from app.grid.allocation import resolve_router_target
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.sessions.protocols import SessionCrudProtocol

OPERATOR_KILL_ERROR_TYPE = "operator_kill"


class SessionNotKillableError(Exception):
    """The session exists but is not a live running session."""


@dataclass(frozen=True, slots=True)
class KillOutcome:
    session: Session
    terminated: bool


async def kill_session(db: AsyncSession, *, crud: SessionCrudProtocol, session_id: str) -> KillOutcome | None:
    """Returns None for an unknown session; raises SessionNotKillableError for
    a row that is not running (pending rows belong to the allocation reaper)."""
    # Own query instead of crud.get_session: resolve_router_target -> node_target
    # touches device.appium_node and device.host, which must be eager-loaded
    # (async lazy-load raises MissingGreenlet). Same most-recent-match semantics
    # as crud.get_session.
    stmt = (
        select(Session)
        .where(Session.session_id == session_id)
        .options(
            selectinload(Session.device).selectinload(Device.appium_node),
            selectinload(Session.device).selectinload(Device.host),
        )
        .order_by(Session.started_at.desc(), Session.id.desc())
        .limit(1)
    )
    session = (await db.execute(stmt)).scalars().first()
    if session is None:
        return None
    if session.status != SessionStatus.running or session.ended_at is not None:
        raise SessionNotKillableError(session_id)

    target = resolve_router_target(session)
    terminated = False
    if target is not None:
        terminated = await appium_direct.terminate_session(target, session_id)

    # Stamp the kill provenance on the identity-map instance, then let the
    # existing terminal-status path do ALL bookkeeping (ended_at, intent revoke,
    # device lock + reconcile, ended event, commit). Pending attribute changes
    # survive its internal re-select and commit together.
    session.error_type = OPERATOR_KILL_ERROR_TYPE
    session.error_message = "killed by operator"
    updated = await crud.update_session_status(db, session_id, SessionStatus.error)
    return KillOutcome(session=updated if updated is not None else session, terminated=terminated)
