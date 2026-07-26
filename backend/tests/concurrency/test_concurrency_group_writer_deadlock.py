"""Concurrent group writers must never abort with a deadlock.

Successor to the deleted ``test_concurrency_group_row_lock_order.py``. That file
pinned a *uniform* lock order — every ``device_groups`` acquisition was one
ascending-key ``FOR UPDATE`` — and asserted a concurrent delete/update pair never
deadlocked. The lock graph has not been uniform since, so absence of deadlock has
to be asserted rather than assumed.

The graph is not confined to ``device_groups`` rows, which is what an earlier
version of this docstring got wrong. It runs through ``device_group_member_of``
*tuple* locks as well, and those are where the only real cycle lives:

* ``update_group(D → [S])`` takes ``FOR UPDATE`` on ``D``, then
  ``DELETE FROM device_group_member_of WHERE dynamic_group_id = D`` — which takes
  the write lock on any committed ``(D, S)`` tuple — and only then asks for
  ``FOR KEY SHARE`` on ``S`` through the reference INSERT's foreign key.
* ``delete_group(S)`` takes the exclusive lock on ``S`` at its
  ``DELETE FROM device_groups``, and the ``ON DELETE RESTRICT`` trigger *then*
  runs ``SELECT 1 FROM device_group_member_of WHERE static_group_id = S
  FOR KEY SHARE`` — wanting the very tuple the updater is holding.

Tuple-then-row against row-then-tuple is an ABBA cycle, and PostgreSQL resolves
it by aborting one side with 40P01. Neither ``_try_delete_group_row`` nor
``_replace_member_of`` catches anything but ``IntegrityError``, so it escapes
untyped: a 500 where the contract promises 409/422. ``delete_group`` therefore
hoists the trigger's own lock — ``_lock_referencing_edges`` takes
``FOR KEY SHARE`` on the referencing tuples *before* the parent row — so both
writers acquire in the same order and no cycle can form.

``add_members``/``remove_members`` take one ``device_groups`` row lock via
``_get_group_row(..., for_update=True)`` and want nothing else in this graph;
they can block a peer but cannot be part of a cycle.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Delete, text
from sqlalchemy.exc import DBAPIError

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services import groups as group_service
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import build_groups_service, fetch_group_rows, fetch_member_of_keys
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# A bound on the whole gather, not a race parameter. Real writers contending for
# one row settle in milliseconds; anything approaching this is a wedge, and a
# legible timeout beats a hung CI job (``pytest-timeout`` is not a dependency).
GATHER_TIMEOUT_SEC = 15.0

# Waits on a coordinating seam. Same reasoning: bounds a seam that stopped
# firing instead of hanging the run.
EVENT_WAIT_TIMEOUT_SEC = 5.0

# How long to wait for a peer backend to reach its blocking statement.
# Comfortably above PostgreSQL's 1 s default ``deadlock_timeout`` so a real
# deadlock is detected and reported rather than mistaken for a slow peer.
PEER_BLOCK_TIMEOUT_SEC = 10.0


async def _wait(flag: asyncio.Event, *, label: str) -> None:
    try:
        await asyncio.wait_for(flag.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        raise AssertionError(f"{label}: the coordinating seam never fired within {EVENT_WAIT_TIMEOUT_SEC}s") from None


async def _wait_until_a_peer_blocks(db_session_maker: async_sessionmaker[AsyncSession], *, label: str) -> None:
    """Return once some other backend in this database is waiting on a lock.

    Deterministic where a sleep would not be: the handoff condition is "the peer
    has reached its blocking statement", which ``pg_stat_activity`` reports as a
    fact rather than something a duration approximates. Each xdist worker owns
    its own database and runs its tests serially, so
    ``datname = current_database()`` scopes this to the sessions under test.
    """
    stmt = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
    )

    async def _poll() -> None:
        while True:
            async with db_session_maker() as watcher:
                blocked = int((await watcher.execute(stmt)).scalar_one())
                await watcher.rollback()
            if blocked:
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(_poll(), timeout=PEER_BLOCK_TIMEOUT_SEC)
    except TimeoutError:
        raise AssertionError(
            f"{label}: no peer backend blocked on a lock within {PEER_BLOCK_TIMEOUT_SEC}s, so the "
            "interleaving under test never happened"
        ) from None


def _park_after_the_edge_delete(
    session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    edge_locked: asyncio.Event,
) -> None:
    """Hold *session* between ``_replace_member_of``'s DELETE and its INSERT.

    That gap is the whole interleaving: the DELETE has taken the write lock on
    the committed ``(D, S)`` tuple and the INSERT has not yet asked for
    ``FOR KEY SHARE`` on ``S``. The seam matches the statement *structurally* —
    a ``Delete`` against ``device_group_member_of`` — rather than by SQL text,
    so rewording the query cannot silently disarm it.
    """
    original_execute = session.execute
    fired = False

    async def _intercepted(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(stmt, *args, **kwargs)
        if not fired and isinstance(stmt, Delete) and stmt.table.name == DeviceGroupMemberOf.__tablename__:
            fired = True
            session.execute = original_execute  # type: ignore[assignment, method-assign]
            edge_locked.set()
            # Only proceed once the deleter is genuinely blocked; otherwise the
            # INSERT wins the race to S and no cycle can form.
            await _wait_until_a_peer_blocks(db_session_maker, label="updater")
        return result

    session.execute = _intercepted  # type: ignore[assignment, method-assign]


async def test_reference_replacement_and_delete_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ABBA cycle in this module's docstring, driven to completion.

    Fully real on both sides — real ``delete_group``, real ``update_group``, a
    real committed edge, real foreign-key triggers. Only the *scheduling* is
    patched, through the same ``_dependent_dynamic_keys`` seam the sibling
    recovery tests use:

    1. the deleter's preflight runs and genuinely finds no referrer;
    2. a peer commits the ``(D, S)`` edge inside that gap — the window the
       recovery path exists for;
    3. the updater re-asserts ``member_of: [S]``, so its ``DELETE`` takes the
       write lock on that committed tuple, and parks before its INSERT;
    4. the deleter proceeds and blocks;
    5. the updater then asks for ``FOR KEY SHARE`` on ``S``.

    Before ``_lock_referencing_edges`` this aborted one side with 40P01, a
    ``DBAPIError`` neither writer catches and the routers do not map — a 500 on
    an interleaving whose documented answers are 409 and 422.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key, dynamic_key = f"static-{suffix}", f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add_all([static, dynamic])
    await db_session.commit()
    static_id, dynamic_id = static.id, dynamic.id

    service = build_groups_service()
    preflight_done = asyncio.Event()
    edge_locked = asyncio.Event()
    original_preflight = group_service._dependent_dynamic_keys
    calls = 0

    async def preflight(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        nonlocal calls
        calls += 1
        result = await original_preflight(db, group_id)
        if calls == 1:
            assert result == [], "the preflight must have run before the edge existed"
            async with db_session_maker() as peer:
                peer.add(DeviceGroupMemberOf(dynamic_group_id=dynamic_id, static_group_id=static_id))
                await peer.commit()
            preflight_done.set()
            await _wait(edge_locked, label="deleter")
        return result

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", preflight)

    async def replace_reference() -> dict[str, Any] | None:
        await _wait(preflight_done, label="updater")
        async with db_session_maker() as session:
            _park_after_the_edge_delete(session, db_session_maker, edge_locked)
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    update_result, delete_result = await asyncio.wait_for(
        asyncio.gather(replace_reference(), delete_static(), return_exceptions=True),
        timeout=GATHER_TIMEOUT_SEC,
    )

    for label, result in (("update_group", update_result), ("delete_group", delete_result)):
        if isinstance(result, AssertionError):
            raise result  # a coordinating seam failed; its message is the real one
        assert not isinstance(result, DBAPIError), (
            f"{label} aborted with an untyped database error. The routers map only GroupReferencedError "
            f"and UnknownMemberOfError, so this reaches the operator as a 500: {result!r}"
        )

    assert calls >= 2, "the deleter never reached its DELETE; the interleaving under test did not happen"
    assert not isinstance(update_result, Exception), f"the updater held the edge and must win: {update_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"the deleter must be refused by the committed edge, got {delete_result!r}"
    )
    assert delete_result.dependents == [dynamic_key]

    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None and dynamic_row is not None
    assert await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key) == [static_key]


async def _seed_writers_fixture(db_session: AsyncSession) -> tuple[str, str, str, str, str]:
    """A delete target, a dynamic group that already references something, and
    two unrelated groups.

    The dynamic group is seeded with a **committed** edge to ``anchor_key``, so
    the reference arm's ``_replace_member_of`` deletes a real tuple and takes its
    write lock rather than writing a first edge into empty space. Without that
    the arm never holds the tuple half of the lock graph, and the delete arm can
    only ever contend on a ``device_groups`` row.

    The delete target itself starts *unreferenced*, which is a different
    requirement and equally deliberate: a referenced target makes
    ``delete_group`` raise ``GroupReferencedError`` from its dependent lookup
    before it reaches the ``DELETE``, so the row write lock is never taken and
    the contention this test exists for never happens. The two together are what
    let both writers reach their statements; which one wins is then a genuine
    race, and both outcomes are asserted.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    anchor_key = f"anchor-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    first_key = f"independent-a-{suffix}"
    second_key = f"independent-b-{suffix}"
    anchor = DeviceGroup(key=anchor_key, name=anchor_key, group_type=GroupType.static)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add_all(
        [
            DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static),
            anchor,
            dynamic,
            DeviceGroup(key=first_key, name=first_key, group_type=GroupType.static),
            DeviceGroup(key=second_key, name=second_key, group_type=GroupType.static),
        ]
    )
    await db_session.flush()
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=dynamic.id, static_group_id=anchor.id))
    await db_session.commit()
    return static_key, anchor_key, dynamic_key, first_key, second_key


async def test_concurrent_group_writers_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """Two independent updates, one reference replacement, a delete, and a
    membership edit all settle without Postgres aborting one as a deadlock
    victim.

    The reference replacement, the delete, and the membership edit meet on the
    same static row, and the replacement additionally holds a committed edge
    tuple on its way there — every lock mode in the module docstring's graph is
    live. The independent updates prove the contention stays confined to it.
    """
    static_key, anchor_key, dynamic_key, first_key, second_key = await _seed_writers_fixture(db_session)
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

    async def replace_reference() -> dict[str, Any] | None:
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
            replace_reference(),
            delete_static(),
            touch_members(),
            return_exceptions=True,
        ),
        timeout=GATHER_TIMEOUT_SEC,
    )

    for result in (first_result, second_result, reference_result, delete_result, members_result):
        assert not isinstance(result, DBAPIError), f"a group writer aborted with a database error: {result!r}"

    # Absence of a database error is not enough on its own: if every writer
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
        assert references == [static_key], f"the replacement must have removed the anchor edge, got {references}"
    else:
        assert delete_result is True, f"no edge landed, so the delete must have succeeded: {delete_result!r}"
        assert isinstance(reference_result, UnknownMemberOfError), (
            f"the reference mutation must be refused once its target is gone, got {reference_result!r}"
        )
        assert reference_result.keys == [static_key]
        assert members_result in (1, None), f"add_members must succeed or find the group gone, got {members_result!r}"
        # The refused replacement had already deleted the anchor edge inside its
        # transaction. Rolling back has to have put it back, or a rejected PATCH
        # silently drops references it never mentioned.
        assert references == [anchor_key], f"a refused replacement must restore the prior edge, got {references}"
