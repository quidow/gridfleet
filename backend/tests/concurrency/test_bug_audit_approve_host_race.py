"""Bug 7: ``approve_host`` succeeds against a host deleted concurrently by reject.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-7``.

``approve_host`` at ``backend/app/hosts/service.py:187-209`` performs
an unlocked SELECT for the host row, then an in-memory mutation
(``host.status = HostStatus.online``) plus commit. A concurrent
``reject_host`` (which deletes the row) interleaved between the
SELECT and the UPDATE leaves ``approve_host`` issuing an UPDATE that
matches zero rows and then a ``db.refresh(host)`` that raises
``ObjectDeletedError`` / ``NoResultFound`` — surfaces as a 500 to
the operator instead of a clean "no longer pending" response.

This test reproduces the race by reordering operations:
  1. Main session runs its SELECT (with the fix, this acquires
     ``FOR UPDATE`` on the row).
  2. The hook fires a side-channel that opens its own session,
     sets a short ``lock_timeout``, and attempts to delete the row.
     - On the fixed branch the side-channel blocks on the
       row lock, hits ``lock_timeout``, and rolls back. Main session
       proceeds to update + commit + refresh the still-existing row
       and ``approve_host`` returns the approved ``Host``.
     - On the bug branch the side-channel deletes the row
       successfully before main session commits. Main's UPDATE
       affects zero rows, ``refresh`` then raises
       ``ObjectDeletedError`` / ``NoResultFound`` and ``approve_host``
       leaks the exception.

Invariant: ``approve_host`` must not raise an unhandled exception
under a concurrent reject. If the row is gone it should return
``None``; if the row survives it should return the approved host.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service import HostCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_approve_host_races_concurrent_reject(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    pending = Host(
        hostname=f"race-{uuid.uuid4().hex[:8]}",
        ip="10.0.99.1",
        os_type=OSType.linux,
        agent_port=5100,
        agent_version="0.3.0",
        status=HostStatus.pending,
    )
    db_session.add(pending)
    await db_session.commit()
    host_id = pending.id

    original_execute = db_session.execute
    triggered = False

    async def _delete_between_select_and_commit(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Fire side-channel AFTER main session's SELECT returns.

        The previous version of this test fired the side-channel BEFORE
        ``original_execute`` ran, which collapsed to "row already gone
        when SELECT happens" — a degenerate case that the buggy code
        path already handled (SELECT returns no row → return None).
        The real bug is "row vanishes between SELECT and UPDATE/refresh",
        which only this ordering exercises.
        """
        nonlocal triggered
        result = await original_execute(stmt, *args, **kwargs)
        stmt_text = str(stmt).lower()
        if not triggered and "from hosts" in stmt_text and "select" in stmt_text:
            triggered = True
            async with db_session_maker() as side:
                try:
                    # Short lock_timeout: on the fixed branch main holds
                    # SELECT ... FOR UPDATE so this DELETE will block,
                    # time out, and roll back cleanly (race prevented).
                    # On the bug branch no lock is held so the DELETE
                    # commits immediately.
                    await side.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    await side.execute(delete(Host).where(Host.id == host_id))
                    await side.commit()
                except DBAPIError:
                    await side.rollback()
        return result

    crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    db_session.execute = _delete_between_select_and_commit  # type: ignore[assignment, method-assign]
    try:
        # A leaked exception here is itself the failure: approve_host must stay
        # crash-free when a concurrent reject deletes the row between the SELECT
        # and the UPDATE/refresh.
        approved = await crud.approve_host(db_session, host_id)
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]

    async with db_session_maker() as side:
        persisted = (await side.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()

    # Outcome must be coherent: approved <=> persisted exists with status=online.
    if approved is not None:
        assert persisted is not None, (
            "approve_host returned an approved Host but the underlying row is gone — phantom success"
        )
        assert persisted.status == HostStatus.online
    # Approval was abandoned cleanly (race lost). DB state may or may not
    # have the row, but if it does it must not be silently flipped online.
    elif persisted is not None:
        assert persisted.status == HostStatus.pending
