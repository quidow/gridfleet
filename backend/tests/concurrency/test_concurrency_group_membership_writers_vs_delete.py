"""What the membership writers' ``FOR UPDATE`` on ``device_groups`` protects.

``add_members`` and ``remove_members`` open with
``_get_group_row(..., for_update=True)``. That lock was long documented as the
serialization point between two concurrent membership edits, with
``test_bug_audit_group_add_members_race.py`` cited as its proof. It is not:
``INSERT ... ON CONFLICT DO NOTHING`` alone makes a duplicate benign, and that
test passes with the lock removed. Two concurrent ``add_members`` calls need
nothing from the lock.

The interleaving that does need it is a concurrent ``delete_group``, which
takes ``FOR UPDATE`` on the same row before deleting it. Holding that row is
what makes the writer's own read authoritative, so a group deleted underneath
it reads as gone rather than as a row that will evaporate before the write
lands:

* ``add_members`` — without the lock the plain read still sees the doomed row
  under READ COMMITTED, so the INSERT proceeds and its foreign-key check takes
  ``FOR KEY SHARE`` on a parent the deleter holds exclusively. When the deleter
  commits, the parent is gone and the check fails:
  ``device_group_memberships_group_id_fkey``, an ``IntegrityError`` the router
  surfaces as a 500. With the lock the writer blocks on the row instead,
  EvalPlanQual re-checks the locked tuple, finds it deleted, and returns no row
  — ``add_members`` returns ``None`` and the router answers 404.
* ``remove_members`` — deleting a child row needs no lock on the parent, so
  there is no 500 to prevent here. Without the lock the DELETE simply matches
  the rows the deleter's ``ON DELETE CASCADE`` is about to remove anyway and
  the caller is told ``{"removed": 0}`` with a 200, as if the group still
  existed and merely held no such members. With the lock it returns ``None``
  and the operator gets the 404 that is true.

Both cases are the "Identity" job ``delete_group``'s own ``FOR UPDATE`` comment
describes, seen from the other side of the race. This file, not the bug-6
duplicate-insert test, is what falsifies removing either lock.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.models.group import DeviceGroup, DeviceGroupMembership, GroupType
from tests.concurrency.group_lock_helpers import build_groups_service
from tests.helpers import create_device, seed_host_named

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# How long the deleter holds its uncommitted DELETE after signalling. Only
# widens the window the membership writer has to reach its own first statement;
# correctness does not depend on the exact value.
HANDOFF_SEC = 0.5

# A bound, not a race parameter: comfortably above HANDOFF_SEC, so a seam that
# stopped firing reads as a legible TimeoutError rather than a hung run.
STAGE_WAIT_TIMEOUT_SEC = 5.0


async def _seed(db_session: AsyncSession) -> tuple[str, uuid.UUID, uuid.UUID]:
    """One static group and one device, committed. Returns (group_key, group_id, device_id)."""
    host = await seed_host_named(db_session, f"member-writer-{uuid.uuid4().hex[:8]}")
    device = await create_device(
        db_session,
        host_id=host.id,
        name="member",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    group = DeviceGroup(key=f"vsdelete-{uuid.uuid4().hex[:8]}", name="vs delete", group_type=GroupType.static)
    db_session.add(group)
    await db_session.commit()
    return group.key, group.id, device.id


async def _delete_holding_the_row(
    db_session_maker: async_sessionmaker[AsyncSession],
    group_key: str,
    staged: asyncio.Event,
) -> bool:
    """Delete the group, signal, then hold the uncommitted DELETE for the handoff.

    The sleep sits inside ``begin()`` on purpose: the commit lands at the block's
    exit, so the membership writer runs its first statement against a row that is
    locked and doomed but still visible to a lock-free read.
    """
    async with db_session_maker() as side, side.begin():
        deleted = await build_groups_service().delete_group(side, group_key)
        staged.set()
        await asyncio.sleep(HANDOFF_SEC)
        return deleted


async def _wait_for_delete_staged(staged: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(staged.wait(), timeout=STAGE_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(
            f"the deleter never staged its uncommitted DELETE within {STAGE_WAIT_TIMEOUT_SEC}s, "
            "so no race was staged and this test proved nothing"
        )


async def test_add_members_losing_to_a_concurrent_delete_is_a_404_not_a_500(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    group_key, _group_id, device_id = await _seed(db_session)
    staged = asyncio.Event()

    async def adder() -> int | None:
        await _wait_for_delete_staged(staged)
        async with db_session_maker() as main, main.begin():
            return await build_groups_service().add_members(main, group_key, [device_id])

    deleted, added = await asyncio.gather(
        _delete_holding_the_row(db_session_maker, group_key, staged), adder(), return_exceptions=True
    )

    assert deleted is True, f"the deleter must win the row it locked first; got {deleted!r}"
    assert not isinstance(added, Exception), (
        "add_members must not raise when the group is deleted underneath it — an IntegrityError here is "
        f"the foreign-key 500 the row lock exists to prevent; got {added!r}"
    )
    assert added is None, (
        f"add_members must report the group as gone (404), not claim it added members to it; got {added!r}"
    )


async def test_remove_members_losing_to_a_concurrent_delete_is_a_404_not_a_zero(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    group_key, group_id, device_id = await _seed(db_session)
    db_session.add(DeviceGroupMembership(group_id=group_id, device_id=device_id))
    await db_session.commit()
    staged = asyncio.Event()

    async def remover() -> int | None:
        await _wait_for_delete_staged(staged)
        async with db_session_maker() as main, main.begin():
            return await build_groups_service().remove_members(main, group_key, [device_id])

    deleted, removed = await asyncio.gather(
        _delete_holding_the_row(db_session_maker, group_key, staged), remover(), return_exceptions=True
    )

    assert deleted is True, f"the deleter must win the row it locked first; got {deleted!r}"
    assert not isinstance(removed, Exception), f"remove_members must not raise; got {removed!r}"
    assert removed is None, (
        "remove_members must report the group as gone (404) rather than a truthful-looking "
        f'"removed 0 of them" against a group that no longer exists; got {removed!r}'
    )
