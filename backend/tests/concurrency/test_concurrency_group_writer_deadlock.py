"""Group definition writers acquire parent rows before edges, always.

Successor to the deleted ``test_concurrency_group_row_lock_order.py``. That file
pinned a *uniform* lock order — every ``device_groups`` acquisition was one
ascending-key ``FOR UPDATE`` — and asserted a concurrent delete/update pair never
deadlocked. The resources are no longer just ``device_groups`` rows, so the
ordering rule had to be restated over both tables rather than abandoned.

The rule is: **``device_groups`` rows before ``device_group_member_of`` tuples,
on every writer.** Left to itself neither writer obeys it, and the two disagree:

* ``update_group(D → [S])`` would take ``FOR UPDATE`` on ``D``, then
  ``DELETE FROM device_group_member_of WHERE dynamic_group_id = D`` — the write
  lock on any committed ``(D, S)`` tuple — and only then ask for ``FOR KEY
  SHARE`` on ``S`` through the reference INSERT's foreign key. Edge, then parent.
* ``delete_group(S)`` would take the exclusive lock on ``S`` at its
  ``DELETE FROM device_groups``, and the ``ON DELETE RESTRICT`` trigger would
  *then* run ``SELECT 1 FROM device_group_member_of WHERE static_group_id = S
  FOR KEY SHARE`` — wanting the very tuple the updater holds. Parent, then edge.

Edge-then-parent against parent-then-edge is an ABBA cycle, and PostgreSQL
resolves it by aborting one side with 40P01 — a plain ``DBAPIError`` that no
writer catches and no router maps, so it reaches the operator as a 500 where the
contract promises 409/422. Both writers therefore hoist their parent
acquisition to the front: ``delete_group`` takes ``FOR UPDATE`` on its target
before it reads anything, and ``_replace_member_of`` takes ``FOR KEY SHARE`` on
its targets before its edge ``DELETE``. Doing only one of the two would invert
the cycle rather than remove it.

The trigger's own acquisition cannot be hoisted instead: it runs after the
parent lock by construction and covers edges that appear later, so an earlier
``FOR KEY SHARE`` on the edges can only ever cover part of the set. That was
tried, and it left the window between the hoist and the ``DELETE`` open.

``delete_group``'s ``FOR UPDATE`` does more than order the locks; it excludes.
No peer can insert an edge to a group being deleted (inserting one needs
``FOR KEY SHARE`` on that row), so the dependent read is authoritative rather
than advisory, and it cannot be deleted and recreated underneath the call
either. Those two consequences are asserted here alongside the deadlock
freedom, because they are the same lock.

``add_members``/``remove_members`` take one ``device_groups`` row lock via
``_get_group_row(..., for_update=True)`` and want nothing else in this graph;
``create_group`` mints its source id in its own transaction and takes only
target locks; the portability importer references only rows it created in the
same transaction. None of them ever holds an edge while wanting a parent, so
none can join either cycle.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services import groups as group_service
from app.devices.services.groups import GroupReferencedError, GroupWriteResult, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import (
    build_groups_service,
    capture_statements,
    fetch_group_rows,
    fetch_member_of_keys,
)
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# How long to wait for a named backend to reach its blocking statement.
# Comfortably above PostgreSQL's 1 s default ``deadlock_timeout`` so a real
# deadlock is detected and reported rather than mistaken for a slow peer. This
# is the largest wait any coordinated test here can legitimately incur.
PEER_BLOCK_TIMEOUT_SEC = 10.0

# Waits on an ``asyncio.Event`` a seam sets. Bounds a seam that stopped firing
# instead of hanging the run (``pytest-timeout`` is not a dependency).
EVENT_WAIT_TIMEOUT_SEC = 5.0

# A wedge detector for the whole gather, so it has to clear every bounded wait
# inside it — otherwise it would fire on a slow handoff rather than on a hang,
# and the failure would name the wrong thing. The uncoordinated test below has
# no such wait and gets the tighter bound: writers contending for one row settle
# in milliseconds there, so anything near it really is a wedge.
COORDINATED_TIMEOUT_SEC = PEER_BLOCK_TIMEOUT_SEC + EVENT_WAIT_TIMEOUT_SEC + 5.0
GATHER_TIMEOUT_SEC = 5.0


async def _wait(flag: asyncio.Event, *, label: str) -> None:
    try:
        await asyncio.wait_for(flag.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        raise AssertionError(f"{label}: the coordinating seam never fired within {EVENT_WAIT_TIMEOUT_SEC}s") from None


async def _backend_pid(session: AsyncSession) -> int:
    """The PostgreSQL backend PID behind *session*'s current transaction.

    Must be read while the transaction the caller cares about is open: a session
    returns its connection to the pool on commit or rollback and may check out a
    different one — and therefore a different backend — for the next statement.
    """
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _blocked_on_a_lock(db_session_maker: async_sessionmaker[AsyncSession], pid: int) -> bool:
    stmt = text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'")
    async with db_session_maker() as watcher:
        blocked = int((await watcher.execute(stmt, {"pid": pid})).scalar_one())
        await watcher.rollback()
    return bool(blocked)


async def _wait_until_backend_blocks(
    db_session_maker: async_sessionmaker[AsyncSession],
    pid_holder: list[int],
    *,
    label: str,
) -> None:
    """Return once the backend named by *pid_holder* is waiting on a lock.

    Deterministic where a sleep would not be: the handoff condition is "that
    session has reached its blocking statement", which ``pg_stat_activity``
    reports as a fact rather than something a duration approximates.

    Keyed on one PID rather than "is anything blocked", which is the difference
    between a guard that fails closed and one that fails open. A test-database
    connection blocked for some unrelated reason would satisfy an
    any-backend predicate and release the waiter early, greening the test
    without the interleaving ever occurring — the one direction a
    race-reproduction guard must never fail in. ``pid_holder`` is a list because
    the PID is captured by the coroutine being waited on, after this call is
    already scheduled.
    """

    async def _poll() -> None:
        while True:
            if pid_holder and await _blocked_on_a_lock(db_session_maker, pid_holder[0]):
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(_poll(), timeout=PEER_BLOCK_TIMEOUT_SEC)
    except TimeoutError:
        raise AssertionError(
            f"{label}: backend {pid_holder or '<never captured>'} did not block on a lock within "
            f"{PEER_BLOCK_TIMEOUT_SEC}s, so the interleaving under test never happened"
        ) from None


async def _wait_until_blocked_or_settled(
    db_session_maker: async_sessionmaker[AsyncSession],
    pid_holder: list[int],
    task_holder: list[asyncio.Task[Any]],
    *,
    reached: asyncio.Event | None = None,
    label: str,
) -> None:
    """Wait for whichever state the build under test actually produces.

    A peer released mid-transaction either runs to the point the test cares
    about or blocks on a lock, and *which* one is the behaviour being asserted —
    so the handoff cannot wait on just one of them without budgeting a sleep for
    the branch that does not happen. Waiting on "blocked, finished, or reached
    its marker" keeps the test deterministic in both directions and lets the
    assertions, rather than the timing, decide whether the build is correct.
    """
    deadline = asyncio.get_running_loop().time() + PEER_BLOCK_TIMEOUT_SEC
    while asyncio.get_running_loop().time() < deadline:
        if reached is not None and reached.is_set():
            return
        if task_holder and task_holder[0].done():
            return
        if pid_holder and await _blocked_on_a_lock(db_session_maker, pid_holder[0]):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{label} neither settled nor blocked; the interleaving under test never happened")


def _park_after_the_edge_delete(
    session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    edge_locked: asyncio.Event,
    deleter_pid: list[int],
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
            # Only proceed once the deleter itself is blocked; otherwise the
            # INSERT wins the race to S and no cycle can form.
            await _wait_until_backend_blocks(db_session_maker, deleter_pid, label="updater")
        return result

    session.execute = _intercepted  # type: ignore[assignment, method-assign]


async def test_no_edge_can_be_committed_once_the_delete_has_started(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusion that makes the dependent read authoritative.

    ``delete_group`` takes ``FOR UPDATE`` on its target *before* the dependent
    read, not merely before the ``DELETE``. That ordering is what turns the read
    from a hint into a decision: inserting a reference requires ``FOR KEY SHARE``
    on the referenced row, which conflicts, so from the moment the lock is held
    no peer can commit an edge to this group and the read sees every edge that
    will ever exist for this delete.

    Seamed on the dependent read itself, one statement *earlier* than the
    sibling test below. Moving the lock to after the read would leave that
    sibling green — its peer would still be excluded, because by then the lock
    is held either way — while reopening the window in which an edge lands
    between a clean read and the ``DELETE``, which is the interleaving the
    deleted savepoint-and-replay machinery used to exist for.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key, dynamic_key = f"static-{suffix}", f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add_all([static, dynamic])
    await db_session.commit()
    static_id, dynamic_id = static.id, dynamic.id

    service = build_groups_service()
    peer_pid: list[int] = []
    arming: list[asyncio.Task[Any]] = []
    original_preflight = group_service._dependent_dynamic_keys
    calls = 0

    async def arm_the_edge() -> None:
        async with db_session_maker() as peer:
            peer_pid.append(await _backend_pid(peer))
            peer.add(DeviceGroupMemberOf(dynamic_group_id=dynamic_id, static_group_id=static_id))
            await peer.commit()

    async def preflight(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            arming.append(asyncio.create_task(arm_the_edge()))
            await _wait_until_backend_blocks(db_session_maker, peer_pid, label="deleter")
        return await original_preflight(db, group_id)

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", preflight)

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            await session.commit()
            return deleted

    delete_result = await asyncio.wait_for(delete_static(), timeout=COORDINATED_TIMEOUT_SEC)
    arm_result = await asyncio.wait_for(
        asyncio.gather(*arming, return_exceptions=True), timeout=COORDINATED_TIMEOUT_SEC
    )

    assert calls >= 1, "the seam never fired"
    assert delete_result is True, f"nothing was ever committed against the target, so it must delete: {delete_result!r}"
    assert isinstance(arm_result[0], IntegrityError), (
        f"the blocked edge must be refused once the target is gone, got {arm_result[0]!r}"
    )
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is None and dynamic_row is not None
    assert await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key) == []


async def test_edge_armed_immediately_before_the_parent_delete_does_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The same cycle, armed one statement later — and by a *different* holder.

    The first fix hoisted a ``FOR KEY SHARE`` onto the referencing edges just
    before the parent ``DELETE``, and argued that edges created after it could
    not head a cycle because their inserter must wait on the parent row this
    caller was "about to" lock. About to is not holds. Between that hoist and
    the ``DELETE`` the deleter held nothing on the parent, so a peer committed an
    edge unimpeded — and a *later* transaction then took that committed edge's
    write lock and wanted the parent, reforming the identical cycle. The
    argument covered the inserting peer and not the next holder of what it
    created.

    The seam here is "immediately before the ``DELETE`` against
    ``device_groups``", which is one statement later than the previous test's
    and is the last moment at which the window can still be open. Under
    parent-before-edge ordering the deleter already holds ``FOR UPDATE`` on the
    target by then, so the peer's INSERT cannot commit at all: it blocks, the
    delete completes, and the INSERT fails against a target that is gone. That
    exclusion is the property replacing the whole preflight-gap recovery path.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key, dynamic_key = f"static-{suffix}", f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add_all([static, dynamic])
    await db_session.commit()
    static_id, dynamic_id = static.id, dynamic.id

    service = build_groups_service()
    parked = asyncio.Event()
    armer_pid: list[int] = []
    deleter_pid: list[int] = []
    armer: list[asyncio.Task[Any]] = []

    async def arm_and_replace() -> GroupWriteResult | None:
        # Commit the edge from a peer, then hand it to a *second* transaction —
        # the holder the previous argument missed.
        async with db_session_maker() as peer:
            armer_pid.append(await _backend_pid(peer))
            peer.add(DeviceGroupMemberOf(dynamic_group_id=dynamic_id, static_group_id=static_id))
            await peer.commit()
        async with db_session_maker() as session:
            _park_after_the_edge_delete(session, db_session_maker, parked, deleter_pid)
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def _hand_over() -> None:
        await _wait_until_blocked_or_settled(
            db_session_maker,
            armer_pid,
            armer,
            reached=parked,
            label="the arming peer",
        )

    def _arm_before_the_parent_delete(session: AsyncSession) -> None:
        original_execute = session.execute
        fired = False

        async def _intercepted(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            nonlocal fired
            if not fired and isinstance(stmt, Delete) and stmt.table.name == DeviceGroup.__tablename__:
                fired = True
                session.execute = original_execute  # type: ignore[assignment, method-assign]
                deleter_pid.append(await _backend_pid(session))
                armer.append(asyncio.create_task(arm_and_replace()))
                await _hand_over()
            return await original_execute(stmt, *args, **kwargs)

        session.execute = _intercepted  # type: ignore[assignment, method-assign]

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            _arm_before_the_parent_delete(session)
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            await session.commit()
            return deleted

    delete_result: bool | BaseException
    try:
        delete_result = await asyncio.wait_for(delete_static(), timeout=COORDINATED_TIMEOUT_SEC)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 — the whole point is what escapes delete_group
        delete_result = exc
    armer_result = await asyncio.wait_for(
        asyncio.gather(*armer, return_exceptions=True), timeout=COORDINATED_TIMEOUT_SEC
    )

    assert armer, "the seam never fired; delete_group issued no DELETE against device_groups"
    assert not isinstance(delete_result, DBAPIError), (
        f"delete_group aborted with an untyped database error. The routers map only GroupReferencedError "
        f"and UnknownMemberOfError, so this reaches the operator as a 500: {delete_result!r}"
    )
    assert delete_result is True, f"the target was unreferenced when the delete ran: {delete_result!r}"

    # The exclusion, stated positively: the edge never landed, because the
    # INSERT could not take FOR KEY SHARE on a row the deleter held FOR UPDATE.
    assert isinstance(armer_result[0], IntegrityError), (
        f"the peer's edge must be refused against a deleted target, got {armer_result[0]!r}"
    )
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is None, "the delete must have completed"
    assert dynamic_row is not None
    assert await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key) == []


def _first_index(statements: list[str], *, predicate: Callable[[str], bool], what: str) -> int:
    index = next((i for i, statement in enumerate(statements) if predicate(statement)), None)
    assert index is not None, f"no statement {what}: {statements}"
    return index


async def test_definition_writers_lock_parents_before_touching_edges(
    db_session: AsyncSession,
) -> None:
    """The ordering rule itself, asserted on the statements each writer issues.

    The other tests here assert *consequences* — no 40P01, no edge slipping past
    a delete. Consequences are the right thing to assert, but they are also why
    the first attempt at this fix looked correct: an argument about one writer's
    behaviour can hold while the rule it was supposed to establish does not.
    Both halves of a lock order have to be in place for either to be worth
    anything, and with ``delete_group`` fixed the deleter's own exclusion makes
    ``_replace_member_of``'s half currently unobservable through outcomes alone.

    So this pins the rule directly: on both writers, the ``device_groups``
    acquisition precedes the first statement that touches
    ``device_group_member_of``. A future writer that reverses either half
    reintroduces the cycle, and would otherwise reintroduce it silently.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key, dynamic_key = f"static-{suffix}", f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add_all([static, dynamic])
    await db_session.flush()
    # A committed edge, so the replacement's DELETE has a real tuple to lock.
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=dynamic.id, static_group_id=static.id))
    await db_session.commit()
    service = build_groups_service()

    def touches_edges(statement: str) -> bool:
        return DeviceGroupMemberOf.__tablename__ in statement.lower()

    async with capture_statements(db_session) as statements:
        updated = await service.update_group(
            db_session,
            dynamic_key,
            DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
        )
    assert updated is not None
    assert db_session.in_transaction(), "update_group must leave its successful write for the caller to commit"
    await db_session.commit()
    lock_index = _first_index(
        statements,
        predicate=lambda s: "for key share" in s.lower() and "device_groups" in s.lower(),
        what="takes FOR KEY SHARE on device_groups",
    )
    edge_index = _first_index(statements, predicate=touches_edges, what="touches device_group_member_of")
    assert lock_index < edge_index, (
        "_replace_member_of touched device_group_member_of before locking its target rows; "
        f"that is the edge-then-parent order delete_group deadlocks against:\n{statements}"
    )

    async with capture_statements(db_session) as statements:
        with pytest.raises(GroupReferencedError):
            await service.delete_group(db_session, static_key)
    lock_index = _first_index(
        statements,
        predicate=lambda s: "for update" in s.lower() and "device_groups" in s.lower(),
        what="takes FOR UPDATE on device_groups",
    )
    edge_index = _first_index(statements, predicate=touches_edges, what="touches device_group_member_of")
    assert lock_index < edge_index, (
        "delete_group read device_group_member_of before locking its target row, so the dependent read "
        f"is not authoritative and an edge can still land behind it:\n{statements}"
    )


async def test_concurrent_duplicate_deletes_report_exactly_one_success(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Two operators deleting the same group: one 204, one 404.

    Without the target lock both callers read the row, both preflight it clean,
    and both issue ``DELETE ... WHERE id = :id``. The loser's statement matches
    zero rows — the winner already removed it — but a zero-row ``DELETE`` is not
    an error, so it committed, published a second
    ``device_group.updated {"action": "deleted"}`` and returned success for work
    it did not do. Under ``FOR UPDATE`` the loser's read blocks, re-checks the
    locked tuple through EvalPlanQual, finds it deleted and returns no row at
    all, which is the 404 the contract already documents.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    await db_session.commit()
    service = build_groups_service()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            await session.commit()
            return deleted

    results = await asyncio.wait_for(
        asyncio.gather(delete_static(), delete_static(), return_exceptions=True),
        timeout=GATHER_TIMEOUT_SEC,
    )

    assert sorted(map(repr, results)) == ["False", "True"], (
        f"exactly one caller may report having deleted the group, got {results!r}"
    )
    static_row, _ = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=static_key)
    assert static_row is None


async def test_a_peer_cannot_delete_and_recreate_the_target_underneath_a_delete(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delete_group`` matches its target by key but removes it by id.

    Without the lock those two identities can drift apart mid-call: a peer that
    deletes the key and recreates it hands the ``DELETE`` an id that no longer
    exists, so it matches nothing while the call still commits, publishes
    ``device_group.updated {"action": "deleted"}`` and returns ``True`` — for a
    group that, by key, is still there. Holding ``FOR UPDATE`` on the row from
    the first read makes the pair inseparable: the peer cannot delete it, so
    there is nothing to recreate.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    db_session.add(static)
    await db_session.commit()
    original_id = static.id

    service = build_groups_service()
    peer_pid: list[int] = []
    peer_task: list[asyncio.Task[Any]] = []
    peer_delete: list[bool] = []
    original_preflight = group_service._dependent_dynamic_keys
    calls = 0

    async def delete_and_recreate() -> None:
        async with db_session_maker() as session:
            peer_pid.append(await _backend_pid(session))
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            peer_delete.append(deleted)
            await session.commit()
        if peer_delete[0]:
            async with db_session_maker() as session:
                await service.create_group(
                    session,
                    DeviceGroupCreate(key=static_key, name=static_key, group_type=GroupType.static),
                )
                assert session.in_transaction(), "create_group must leave its successful write for the caller to commit"
                await session.commit()

    async def preflight(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            peer_task.append(asyncio.create_task(delete_and_recreate()))
            await _wait_until_blocked_or_settled(db_session_maker, peer_pid, peer_task, label="the recreating peer")
        return await original_preflight(db, group_id)

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", preflight)

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            await session.commit()
            return deleted

    delete_result = await asyncio.wait_for(delete_static(), timeout=COORDINATED_TIMEOUT_SEC)
    await asyncio.wait_for(asyncio.gather(*peer_task), timeout=COORDINATED_TIMEOUT_SEC)

    assert calls >= 1, "the seam never fired"
    assert delete_result is True, f"this caller held the row and must have removed it: {delete_result!r}"
    assert peer_delete == [False], (
        f"the peer deleted the target out from under a call that then reported success for it; got {peer_delete!r}"
    )
    async with db_session_maker() as verify:
        surviving = (
            await verify.execute(select(DeviceGroup.id).where(DeviceGroup.key == static_key))
        ).scalar_one_or_none()
    assert surviving is None, (
        f"the key still resolves after a delete that reported success (row {surviving!r}, originally {original_id!r})"
    )


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

    async def touch(group_key: str) -> GroupWriteResult | None:
        async with db_session_maker() as session:
            updated = await service.update_group(
                session,
                group_key,
                DeviceGroupUpdate(description=f"touched-{uuid.uuid4().hex[:6]}"),
            )
            assert session.in_transaction(), "update_group must leave its successful write for the caller to commit"
            await session.commit()
            return updated

    async def replace_reference() -> GroupWriteResult | None:
        async with db_session_maker() as session:
            updated = await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )
            assert session.in_transaction(), "update_group must leave its successful write for the caller to commit"
            await session.commit()
            return updated

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            deleted = await service.delete_group(session, static_key)
            assert session.in_transaction(), "delete_group must leave its successful write for the caller to commit"
            await session.commit()
            return deleted

    async def touch_members() -> int | None:
        # A real device, so add_members takes its ``FOR UPDATE`` row lock and
        # holds it through an actual insert. That lock, not the
        # insert, is what could form a cycle — an empty device list now
        # short-circuits and would barely contend.
        async with db_session_maker() as session:
            added = await service.add_members(session, static_key, [device_id])
            await session.commit()
            return added

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
        assert isinstance(result, GroupWriteResult), f"an update on an uncontended group must commit, got {result!r}"
        assert result.payload["key"] == key

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
