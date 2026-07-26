"""Concurrent group writers must never abort with a deadlock.

Successor to the deleted ``test_concurrency_group_row_lock_order.py``. That file
pinned a *uniform* lock order — every ``device_groups`` acquisition was one
ascending-key ``FOR UPDATE`` — and asserted a concurrent delete/update pair never
deadlocked. The lock graph has not been uniform since, so absence of deadlock is
worth asserting rather than assuming.

The asymmetry is deliberate and is the thing to watch. Nothing serialises group
definition writers as a class any more, so every arm below acquires whatever the
statements it issues happen to need:

* ``update_group`` writing a ``member_of`` reference inserts into
  ``device_group_member_of``, which takes ``FOR KEY SHARE`` on the target row;
* ``delete_group`` takes the conflicting lock on that same row at its ``DELETE``
  (and, only on the recovery path, one ``FOR UPDATE`` on the row it replays);
* ``add_members`` takes ``FOR UPDATE`` via ``_get_group_row(..., for_update=True)``
  and touches ``device_group_memberships`` under it;
* an update that touches no reference locks only the row it is updating.

No cycle exists today, because no writer requests a second ``device_groups``
lock while holding the first. A future change that does — a membership writer
that resolves a reference under its row lock, say — reintroduces one, and every
other test in this directory would stay green.

The outcome assertions are the same two-state invariant the reference races use:
independent updates may all commit, but the reference/delete pair has to agree.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import build_groups_service, fetch_group_rows, fetch_member_of_keys
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# A bound on the whole gather, not a race parameter. Five real writers contending
# for one row settle in milliseconds; anything approaching this is a wedge, and a
# legible timeout beats a hung CI job (``pytest-timeout`` is not a dependency).
GATHER_TIMEOUT_SEC = 5.0


async def _seed_writers_fixture(db_session: AsyncSession) -> tuple[str, str, str, str]:
    """A delete target, a dynamic group to reference it, and two unrelated groups.

    The static target starts with no referrer on purpose. A referenced group
    makes ``delete_group`` raise ``GroupReferencedError`` from its dependent
    lookup *before* it reaches the ``DELETE`` — so the row write lock is never
    taken and the contention this file exists to test never happens. The delete
    arm has to be able to reach its statement.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    first_key = f"independent-a-{suffix}"
    second_key = f"independent-b-{suffix}"
    db_session.add_all(
        [
            DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static),
            DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic),
            DeviceGroup(key=first_key, name=first_key, group_type=GroupType.static),
            DeviceGroup(key=second_key, name=second_key, group_type=GroupType.static),
        ]
    )
    await db_session.commit()
    return static_key, dynamic_key, first_key, second_key


async def test_concurrent_group_writers_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """Two independent updates, one reference mutation, a delete, and a membership
    edit all settle without Postgres aborting one as a deadlock victim.

    The reference mutation, the delete, and the membership edit target the *same*
    static row on purpose: that is where all three lock modes meet. The
    independent updates are there to prove the contention is confined to it.
    """
    static_key, dynamic_key, first_key, second_key = await _seed_writers_fixture(db_session)
    host = await create_host(client)
    device = await create_device(db_session, host_id=uuid.UUID(host["id"]), name=f"dl-{uuid.uuid4().hex[:6]}")
    await db_session.commit()
    device_id = device.id
    service = build_groups_service()

    async def touch(group_key: str) -> dict[str, Any] | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                group_key,
                DeviceGroupUpdate(description=f"touched-{uuid.uuid4().hex[:6]}"),
            )

    async def add_reference() -> dict[str, Any] | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    async def touch_members() -> int | None:
        # A real device, so add_members takes its ``FOR UPDATE`` row lock and
        # holds it through an actual insert and commit. That lock, not the
        # insert, is what could form a cycle — an empty device list now
        # short-circuits and would barely contend.
        async with db_session_maker() as session:
            return await service.add_members(session, static_key, [device_id])

    first_result, second_result, reference_result, delete_result, members_result = await asyncio.wait_for(
        asyncio.gather(
            touch(first_key),
            touch(second_key),
            add_reference(),
            delete_static(),
            touch_members(),
            return_exceptions=True,
        ),
        timeout=GATHER_TIMEOUT_SEC,
    )

    for result in (first_result, second_result, reference_result, delete_result, members_result):
        if isinstance(result, Exception):
            assert "deadlock" not in str(result).lower(), f"group writers deadlocked: {result!r}"

    # Absence of the word "deadlock" is not enough on its own: if every writer
    # failed for some unrelated reason, the loop above would still pass.
    for key, result in ((first_key, first_result), (second_key, second_result)):
        assert isinstance(result, dict), f"an update on an uncontended group must commit, got {result!r}"
        assert result["key"] == key

    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    references = await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key)
    static_exists = static_row is not None
    relation_exists = static_key in references

    assert dynamic_row is not None, "the referring dynamic group is not a delete target"
    assert not (static_exists is False and relation_exists is True), (
        f"{dynamic_key} references deleted static group {static_key}"
    )
    assert (static_exists, relation_exists) in {(True, True), (False, False)}, (
        f"inconsistent end state: static_exists={static_exists} relation_exists={relation_exists}"
    )

    if relation_exists:
        assert not isinstance(reference_result, Exception), f"the edge landed: {reference_result!r}"
        assert isinstance(delete_result, GroupReferencedError), (
            f"deleter must be refused once the edge exists, got {delete_result!r}"
        )
        assert delete_result.dependents == [dynamic_key]
        assert members_result == 1, f"add_members must land on a surviving group, got {members_result!r}"
    else:
        assert delete_result is True, f"no edge landed, so the delete must have succeeded: {delete_result!r}"
        assert isinstance(reference_result, UnknownMemberOfError), (
            f"the reference mutation must be refused once its target is gone, got {reference_result!r}"
        )
        assert reference_result.keys == [static_key]
        assert members_result in (1, None), f"add_members must succeed or find the group gone, got {members_result!r}"
