"""Bug 7: ``approve_host`` succeeds against a host deleted concurrently by reject.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-7``.

``approve_host`` at ``backend/app/hosts/service.py:187-209`` performs
an unlocked SELECT for the host row, then an in-memory mutation
(``host.status = HostStatus.online``) plus commit. A concurrent
``reject_host`` (which deletes the row) interleaved between the
SELECT and the UPDATE leaves the function returning a "successful"
``Host`` object with ``status=online`` even though the row no longer
exists — the UPDATE silently affects zero rows and ``return host``
hands the caller a phantom object.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service import approve_host

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

    async def _delete_after_select(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal triggered
        stmt_text = str(stmt).lower()
        # First SELECT against hosts table inside approve_host: simulate a
        # concurrent reject_host that deletes the pending row immediately
        # BEFORE we observe the row. The side-channel commits before the
        # main session's SELECT runs so the main session sees no row.
        if not triggered and "from hosts" in stmt_text and "select" in stmt_text:
            triggered = True
            async with db_session_maker() as side:
                victim = await side.get(Host, host_id)
                if victim is not None:
                    await side.delete(victim)
                    await side.commit()
        return await original_execute(stmt, *args, **kwargs)

    db_session.execute = _delete_after_select  # type: ignore[assignment, method-assign]
    try:
        approved = await approve_host(db_session, host_id)
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]

    # Fixed behavior: approve_host should detect the row no longer exists
    # (UPDATE affected zero rows, or re-SELECT after locking returns None)
    # and return None — i.e. "the host is no longer pending; nothing to
    # approve." Current behavior (bug): the function returns a Host
    # object with status=online even though no DB row backs it.
    async with db_session_maker() as side:
        persisted = (await side.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()

    if approved is not None and persisted is None:
        pytest.fail(
            "approve_host returned a 'successful' approved host but the underlying "
            "row was deleted by a concurrent reject — caller sees status=online for "
            "a host that no longer exists"
        )
