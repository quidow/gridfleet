"""Concurrent group writers must never abort with a deadlock.

Successor to the deleted ``test_concurrency_group_row_lock_order.py``. That file
pinned a *uniform* lock order — every ``device_groups`` acquisition was one
ascending-key ``FOR UPDATE`` — and asserted a concurrent delete/update pair
never deadlocked. The advisory lock retired the ordering discipline, but not
the property: the lock graph is no longer uniform, so absence of deadlock is
worth asserting rather than assuming.

The asymmetry is deliberate and is the thing to watch:

* ``create_group`` / ``update_group`` / ``delete_group`` take the advisory lock
  first and touch ``device_groups`` rows only afterwards (``delete_group``'s
  ``DELETE`` takes a row write lock at flush).
* ``add_members`` / ``remove_members`` take a ``device_groups`` row lock via
  ``_get_group_row(..., for_update=True)`` and never take the advisory lock.

No cycle exists today, because the membership writers never request the
advisory lock while holding a row lock. A future change that gives them one
after their row lock — or that adds a second row-lock acquisition under the
advisory lock — reintroduces one, and every other test in this directory would
stay green.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from tests.concurrency.group_lock_helpers import build_groups_service
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _seed_unreferenced_static(db_session: AsyncSession) -> str:
    """A static group with no ``member_of`` referrer.

    Unreferenced on purpose. A referenced group makes ``delete_group`` raise
    ``GroupReferencedError`` from ``_assert_no_references`` *before* it ever
    reaches ``db.delete(group)`` — so the DELETE's row write lock is never taken
    and the delete-versus-membership contention this file exists to test never
    happens. The delete arm has to be able to reach its flush.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    await db_session.commit()
    return static_key


async def test_concurrent_group_writers_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """A delete, an update, and a membership edit racing the same group row all
    settle without Postgres aborting one as a deadlock victim.

    All three target the *same* row on purpose. The delete reaches its DELETE
    flush (the group has no referrer to reject it), so its row write lock
    genuinely contends with ``add_members``' ``FOR UPDATE`` while both
    ``delete_group`` and ``update_group`` hold the advisory lock in turn — the
    cycle shape described in the module docstring.
    """
    static_key = await _seed_unreferenced_static(db_session)
    host = await create_host(client)
    device = await create_device(db_session, host_id=uuid.UUID(host["id"]), name=f"dl-{uuid.uuid4().hex[:6]}")
    await db_session.commit()
    device_id = device.id
    service = build_groups_service()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    async def rename_static() -> DeviceGroup | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                static_key,
                DeviceGroupUpdate(description=f"touched-{uuid.uuid4().hex[:6]}"),
            )

    async def touch_members() -> int | None:
        # A real device, so add_members takes its ``FOR UPDATE`` row lock and
        # holds it through an actual insert and commit. That lock, not the
        # insert, is what could form a cycle — an empty device list now
        # short-circuits and would barely contend.
        async with db_session_maker() as session:
            return await service.add_members(session, static_key, [device_id])

    delete_result, update_result, members_result = await asyncio.gather(
        delete_static(), rename_static(), touch_members(), return_exceptions=True
    )

    for result in (delete_result, update_result, members_result):
        if isinstance(result, Exception):
            assert "deadlock" not in str(result).lower(), f"group writers deadlocked: {result!r}"

    # Absence of the word "deadlock" is not enough on its own: if every writer
    # failed for some unrelated reason, the loop above would still pass. Pin each
    # outcome to the set that is legitimate for *this* interleaving, where the
    # delete is unopposed and therefore must succeed.
    assert delete_result is True, f"the delete must reach its flush and succeed, got {delete_result!r}"
    # Either ordering is legitimate: the update lands if it won the advisory
    # lock, or returns None once the delete has removed the row.
    assert isinstance(update_result, DeviceGroup) or update_result is None, (
        f"the update must land or find the group gone, got {update_result!r}"
    )
    # 1 if it took the row lock before the delete's flush, None if the delete
    # committed first. Anything else means it failed for another reason.
    assert members_result in (1, None), f"add_members must succeed or find the group gone, got {members_result!r}"
