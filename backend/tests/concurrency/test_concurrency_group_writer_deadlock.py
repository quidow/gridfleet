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
from app.devices.services.groups import GroupReferencedError
from tests.concurrency.group_lock_helpers import build_groups_service
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _seed_referenced_pair(db_session: AsyncSession) -> tuple[str, str]:
    """A static group and a dynamic group already referencing it.

    Keys are chosen so the dynamic key sorts *before* the static one: under the
    retired discipline that ordering was what a reference-directed lock order
    would have inverted, so it remains the shape most likely to expose a cycle.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"z-static-{suffix}"
    dynamic_key = f"a-dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"member_of": [static_key], "device_type": "real_device"},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key


async def test_concurrent_group_writers_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """A delete, an update, and a membership edit racing the same pair of rows
    all settle without Postgres aborting one as a deadlock victim."""
    static_key, dynamic_key = await _seed_referenced_pair(db_session)
    host = await create_host(client)
    device = await create_device(db_session, host_id=uuid.UUID(host["id"]), name=f"dl-{uuid.uuid4().hex[:6]}")
    await db_session.commit()
    device_id = device.id
    service = build_groups_service()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    async def update_dynamic() -> DeviceGroup | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(description=f"touched-{uuid.uuid4().hex[:6]}"),
            )

    async def touch_members() -> int | None:
        # A real device, so add_members takes its ``FOR UPDATE`` row lock and
        # holds it through an actual insert and commit. That lock, not the
        # insert, is what could form a cycle — an empty device list would
        # release it immediately and barely contend.
        async with db_session_maker() as session:
            return await service.add_members(session, static_key, [device_id])

    delete_result, update_result, members_result = await asyncio.gather(
        delete_static(), update_dynamic(), touch_members(), return_exceptions=True
    )

    for result in (delete_result, update_result, members_result):
        if isinstance(result, Exception):
            assert "deadlock" not in str(result).lower(), f"group writers deadlocked: {result!r}"

    # Absence of the word "deadlock" is not enough on its own: if every writer
    # failed for some unrelated reason, the loop above would still pass. Pin
    # each outcome to the set that is actually legitimate here.
    assert delete_result is True or isinstance(delete_result, GroupReferencedError), (
        f"delete must either succeed or lose to the live reference, got {delete_result!r}"
    )
    assert isinstance(update_result, DeviceGroup), f"the update must land, got {update_result!r}"
    # 1 if the group survived to receive the member, None if the delete won and
    # add_members found nothing. Anything else means it failed for another reason.
    assert members_result in (1, None), f"add_members must succeed or find the group gone, got {members_result!r}"
