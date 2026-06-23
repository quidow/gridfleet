"""Bug 7 (symmetric): ``reject_host`` deletes a host while approve concurrently flips it online.

Sibling of ``test_bug_audit_approve_host_race``. Same TOCTOU root
cause: ``reject_host`` (pre-fix) issued an unlocked SELECT, checked
``status == pending``, then committed a delete. A concurrent
``approve_host`` could land ``status=online`` between the SELECT
and the DELETE — main session has a stale ``pending`` snapshot in
memory, passes the predicate, and deletes a host that an operator
just approved. Both calls report success; the approval is silently
nuked.

The fix on this branch is ``SELECT ... FOR UPDATE`` on ``reject_host``,
mirroring ``approve_host``. This test reproduces the interleaving:

  1. Main session runs its SELECT (with the fix, ``FOR UPDATE``).
  2. The hook fires a side-channel that opens its own session,
     sets a short ``lock_timeout``, and attempts to flip
     ``status=online``.
     - Fixed branch: main holds the row lock. Side-channel's
       UPDATE blocks, hits ``lock_timeout``, rolls back. The
       attempted "approve" is observably lost. Main proceeds to
       delete the still-pending row and returns ``True``.
     - Bug branch: no lock. Side-channel's UPDATE commits
       immediately. Main session has a stale ``pending`` snapshot
       in memory, passes the predicate, and deletes the row.
       Both report success — operator-issued approval is silently
       discarded.

Invariant: at most one of (concurrent approve, main reject) may
report a successful outcome. If reject reports True, no concurrent
approve may also have landed.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service import HostCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_reject_host_races_concurrent_approve(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    pending = Host(
        hostname=f"race-{uuid.uuid4().hex[:8]}",
        ip="10.0.99.4",
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
    side_approved = False

    async def _approve_between_select_and_commit(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal triggered, side_approved
        result = await original_execute(stmt, *args, **kwargs)
        stmt_text = str(stmt).lower()
        if not triggered and "from hosts" in stmt_text and "select" in stmt_text:
            triggered = True
            async with db_session_maker() as side:
                try:
                    await side.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    update_result = await side.execute(
                        update(Host)
                        .where(Host.id == host_id, Host.status == HostStatus.pending)
                        .values(status=HostStatus.online)
                    )
                    await side.commit()
                    if (update_result.rowcount or 0) > 0:
                        side_approved = True
                except DBAPIError:
                    await side.rollback()
        return result

    crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    db_session.execute = _approve_between_select_and_commit  # type: ignore[assignment, method-assign]
    try:
        # A leaked exception here is itself the failure: reject_host must stay
        # crash-free when a concurrent approve commits between SELECT and commit.
        rejected = await crud.reject_host(db_session, host_id)
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]

    if rejected and side_approved:
        pytest.fail(
            "reject_host returned True AND a concurrent approve committed status=online — "
            "operator-issued approval was silently deleted by the race"
        )
