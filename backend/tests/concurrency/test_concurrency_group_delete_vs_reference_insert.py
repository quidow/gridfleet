"""``delete_group``'s dependent read and its ``DELETE`` are two statements, and a
reference committed between them is invisible to the first and fatal to the second.

The ``ON DELETE RESTRICT`` foreign key on ``device_group_member_of`` is what
closes that window, and this file exercises the branch that translates it. Two
properties are pinned, because getting either wrong turns a documented 409 into
a 500:

* the violation surfaces from the ``DELETE``'s own ``execute`` — an
  ``ON DELETE RESTRICT`` constraint is a non-deferrable AFTER ROW trigger that
  fires at the end of the statement that armed it, so a ``try`` wrapped around a
  following ``flush`` never sees it;
* the recovery reads the session it just rolled back, so the rollback has to be
  to a SAVEPOINT (keeping the transaction and its loaded rows) rather than to
  the root.

Both interleavings are driven by committing real rows from a second session at a
known point, so the ``DELETE`` hits the genuine RESTRICT trigger. Nothing here
patches the failure itself — only the point at which the peer commits.

This branch used to be unreachable in practice: the group-mutation advisory lock
kept every in-app writer off it, which is also why neither of its two recovery
mechanisms was pinned by anything. Both are load-bearing now that the foreign
keys are the only authority, so the second half of this module ablation-tests
them directly — the savepoint, the ``_replace_member_of`` twin of the same
defect, and the identity the recovery locks on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import DBAPIError

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.services import groups as group_service
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import build_groups_service, capture_statements

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@dataclass(frozen=True)
class _Pair:
    """Keys and ids read out of the ORM rows up front.

    Deliberately plain values: ``delete_group`` rolls back on the rejection path,
    a rollback expires every loaded row, and an assertion reading
    ``group.key`` afterwards would need IO from the synchronous assert — the same
    MissingGreenlet trap the service guards against internally.
    """

    static_key: str
    static_id: uuid.UUID
    dynamic_key: str
    dynamic_id: uuid.UUID


async def _seed_unreferenced_pair(db_session: AsyncSession) -> _Pair:
    """A static target and a dynamic source with *no* relation row between them."""
    suffix = uuid.uuid4().hex[:8]
    static = DeviceGroup(key=f"static-{suffix}", name="static", group_type=GroupType.static)
    dynamic = DeviceGroup(key=f"dynamic-{suffix}", name="dynamic", group_type=GroupType.dynamic)
    db_session.add_all([static, dynamic])
    await db_session.commit()
    return _Pair(static_key=static.key, static_id=static.id, dynamic_key=dynamic.key, dynamic_id=dynamic.id)


def _commit_reference_inside_the_gap(
    monkeypatch: pytest.MonkeyPatch,
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    dynamic_group_id: uuid.UUID,
    static_group_id: uuid.UUID,
    withdraw_before_reread: bool,
) -> list[list[str]]:
    """Commit the relation row from a peer session once the dependent read has
    returned, and optionally withdraw it again before the re-read.

    The seam is the *scheduling*, not the failure: the row is a real committed
    row and the ``DELETE`` that follows hits the real foreign key.
    """
    original = group_service._dependent_dynamic_keys
    observed: list[list[str]] = []

    async def _insert() -> None:
        async with db_session_maker() as peer:
            peer.add(DeviceGroupMemberOf(dynamic_group_id=dynamic_group_id, static_group_id=static_group_id))
            await peer.commit()

    async def _withdraw() -> None:
        async with db_session_maker() as peer:
            await peer.execute(
                delete(DeviceGroupMemberOf).where(DeviceGroupMemberOf.dynamic_group_id == dynamic_group_id)
            )
            await peer.commit()

    async def probe(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        call = len(observed) + 1
        if call == 2 and withdraw_before_reread:
            await _withdraw()
        result = await original(db, group_id)
        observed.append(result)
        if call == 1:
            await _insert()
        return result

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", probe)
    return observed


async def test_reference_committed_inside_the_gap_becomes_a_named_409(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The foreign key fires, and the recovery names the referrer.

    Without the fix this raised the raw ``IntegrityError`` past the router, which
    maps only ``GroupReferencedError`` — a 500 where the contract says 409.
    """
    pair = await _seed_unreferenced_pair(db_session)
    observed = _commit_reference_inside_the_gap(
        monkeypatch,
        db_session_maker,
        dynamic_group_id=pair.dynamic_id,
        static_group_id=pair.static_id,
        withdraw_before_reread=False,
    )

    with pytest.raises(GroupReferencedError) as exc:
        await build_groups_service().delete_group(db_session, pair.static_key)

    assert exc.value.dependents == [pair.dynamic_key]
    assert str(exc.value).endswith(pair.dynamic_key), "the 409 must name the referrer, not trail off"
    assert observed[0] == [], "the pre-check must have seen nothing, or the gap was never entered"
    assert observed[1] == [pair.dynamic_key]

    async with db_session_maker() as verify:
        stmt = select(DeviceGroup.key).where(DeviceGroup.id == pair.static_id)
        assert (await verify.execute(stmt)).scalar_one() == pair.static_key, (
            "a rejected delete must leave the target in place"
        )


async def test_reference_withdrawn_inside_the_gap_deletes_instead_of_a_hollow_409(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A referrer that appears and vanishes must not yield ``dependents == []``.

    Re-reading after the violation is the only way to name a referrer, and it can
    legitimately come back empty. Reporting that as a 409 would hand the operator
    a conflict with nobody in it; nothing references the group any more, so the
    delete that lost the race is simply replayed.
    """
    pair = await _seed_unreferenced_pair(db_session)
    observed = _commit_reference_inside_the_gap(
        monkeypatch,
        db_session_maker,
        dynamic_group_id=pair.dynamic_id,
        static_group_id=pair.static_id,
        withdraw_before_reread=True,
    )

    assert await build_groups_service().delete_group(db_session, pair.static_key) is True

    assert observed[0] == [] and observed[1] == [], "the interleaving under test never happened"
    async with db_session_maker() as verify:
        target = select(DeviceGroup.id).where(DeviceGroup.id == pair.static_id)
        assert (await verify.execute(target)).scalar_one_or_none() is None, "the replay must have landed"
        source = select(DeviceGroup.id).where(DeviceGroup.id == pair.dynamic_id)
        assert (await verify.execute(source)).scalar_one_or_none() == pair.dynamic_id, "only the target was deleted"


async def _seed_referenced_pair(db_session: AsyncSession) -> _Pair:
    """A static target a dynamic source already references through a committed edge."""
    pair = await _seed_unreferenced_pair(db_session)
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=pair.dynamic_id, static_group_id=pair.static_id))
    await db_session.commit()
    return pair


async def test_restrict_violation_rolls_back_only_to_a_savepoint(db_session: AsyncSession) -> None:
    """``_try_delete_group_row`` must not take the caller's transaction down with it.

    A root rollback would work by accident: the re-read that names the referrer
    would autobegin a fresh transaction and still find the row. What it destroys
    is everything the caller had already established — the transaction itself,
    and every attribute on every loaded row, so a recovery path that reads one
    gets ``MissingGreenlet`` from a synchronous context instead of an answer.

    Ablating the savepoint (``begin_nested`` replaced by a root ``rollback``)
    fails on the first assertion, which is what this test exists for: neither
    recovery mechanism had a test before this phase made them load-bearing.
    """
    pair = await _seed_referenced_pair(db_session)
    dynamic = await db_session.get(DeviceGroup, pair.dynamic_id)
    assert dynamic is not None
    dynamic.description = "staged before the failing DELETE"
    await db_session.flush()

    assert await group_service._try_delete_group_row(db_session, pair.static_id) is False

    assert db_session.in_transaction(), (
        "the RESTRICT violation rolled the caller's transaction back to the root, not to a SAVEPOINT"
    )
    assert not inspect(dynamic).expired, "a savepoint rollback must leave a clean loaded row populated"
    assert dynamic.description == "staged before the failing DELETE", (
        "work staged before the failed DELETE must survive the savepoint rollback"
    )
    await db_session.rollback()


async def test_reference_insert_violation_rolls_back_only_to_a_savepoint(db_session: AsyncSession) -> None:
    """``_replace_member_of`` is the same defect in the opposite direction.

    It writes the edge rather than deleting the target, but it catches the same
    class of foreign-key violation and used to answer it with a root
    ``db.rollback()`` — silently discarding whatever its caller had staged, from
    inside a helper the caller cannot see into. ``update_group`` stages a field
    update and a queued event before calling it.
    """
    pair = await _seed_unreferenced_pair(db_session)
    dynamic = await db_session.get(DeviceGroup, pair.dynamic_id)
    assert dynamic is not None
    # A target row that never existed: the composite FK on (id, group_type) has
    # nothing to resolve, so the INSERT raises the real violation rather than a
    # patched one.
    ghost = DeviceGroup(
        id=uuid.uuid4(),
        key=f"ghost-{uuid.uuid4().hex[:8]}",
        name="ghost",
        group_type=GroupType.static,
    )

    with pytest.raises(UnknownMemberOfError) as exc:
        await group_service._replace_member_of(db_session, pair.dynamic_id, {ghost.key: ghost})

    assert exc.value.keys == [ghost.key]
    assert db_session.in_transaction(), (
        "a rejected reference INSERT rolled the caller's transaction back to the root, not to a SAVEPOINT"
    )
    assert not inspect(dynamic).expired, "a savepoint rollback must leave a clean loaded row populated"
    assert dynamic.key == pair.dynamic_key
    await db_session.rollback()


def _probe_key_lock_before_commit(
    session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    key: str,
) -> list[str]:
    """Record whether *key*'s row is locked by *session* at its commit boundary.

    The commit is the only point present on both sides of the fix: when the
    recovery locks by id it returns as soon as the id is gone, so there is no
    post-lock seam inside ``_delete_group_or_dependents`` to hook. Probing with
    ``FOR UPDATE NOWAIT`` from a third session makes the answer deterministic —
    no polling, no sleep.
    """
    original_commit = session.commit
    observed: list[str] = []

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        async with db_session_maker() as peer:
            try:
                await peer.execute(select(DeviceGroup.id).where(DeviceGroup.key == key).with_for_update(nowait=True))
            except DBAPIError:
                observed.append("locked")
            else:
                observed.append("free")
            await peer.rollback()
        return await original_commit(*args, **kwargs)

    session.commit = _intercepted  # type: ignore[assignment, method-assign]
    return observed


async def test_recovery_locks_the_row_it_replays(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery's lock and its replay must name the same row.

    The recovery used to take ``FOR UPDATE`` on the row matching *key* while the
    replay deleted by *id*. A peer that deletes and recreates the same key
    inside the window makes those two different rows: the lock lands on a
    recreated group this delete has no business touching and holds it until
    commit, while the guarantee it was there to provide — that no peer can
    re-arm the foreign key on the row about to be deleted — no longer covers
    anything.

    Unreachable while the advisory lock excluded peers. Reachable now, so the
    lock is taken on the id.
    """
    pair = await _seed_unreferenced_pair(db_session)
    original = group_service._dependent_dynamic_keys
    calls = 0
    recreated_id: list[uuid.UUID] = []

    async def _peer(fn: Any) -> None:  # noqa: ANN401
        async with db_session_maker() as peer:
            await fn(peer)
            await peer.commit()

    async def probe(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            # Withdraw the reference, then delete and recreate the target under
            # the same key. The re-read that follows sees no dependent, so the
            # recovery proceeds to its lock — against a key that now resolves to
            # a different row than the one it is about to delete.
            async def _swap(peer: AsyncSession) -> None:
                await peer.execute(
                    delete(DeviceGroupMemberOf).where(DeviceGroupMemberOf.dynamic_group_id == pair.dynamic_id)
                )
                await peer.execute(delete(DeviceGroup).where(DeviceGroup.id == pair.static_id))
                replacement = DeviceGroup(key=pair.static_key, name="recreated", group_type=GroupType.static)
                peer.add(replacement)
                await peer.flush()
                recreated_id.append(replacement.id)

            await _peer(_swap)
        result = await original(db, group_id)
        if calls == 1:

            async def _arm(peer: AsyncSession) -> None:
                peer.add(
                    DeviceGroupMemberOf(dynamic_group_id=pair.dynamic_id, static_group_id=pair.static_id),
                )

            await _peer(_arm)
        return result

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", probe)

    async with db_session_maker() as session:
        observed = _probe_key_lock_before_commit(session, db_session_maker, pair.static_key)
        async with capture_statements(session) as statements:
            assert await build_groups_service().delete_group(session, pair.static_key) is True

    assert calls >= 2, "the DELETE never hit the foreign key; the interleaving under test did not happen"
    assert recreated_id, "the peer never recreated the target"

    # Two independent readings of the same property. The statement text says the
    # recovery asked for the lock by id; the NOWAIT probe says nothing else ended
    # up locked as a result. Either alone would pass on a plausible mistake — a
    # correctly-keyed lock that is never reached, or a key-keyed lock in a run
    # where the key happens to resolve to the same row.
    row_locks = [statement for statement in statements if "for update" in statement.lower()]
    assert row_locks, "the recovery never reached its lock; the interleaving under test did not happen"
    assert not any("device_groups.key" in statement for statement in row_locks), (
        f"the recovery locked by key while the replay deletes by id: {row_locks}"
    )
    assert all("device_groups.id" in statement for statement in row_locks), (
        f"the recovery's lock must name the id it replays: {row_locks}"
    )
    assert observed == ["free"], (
        "delete_group held a row lock on the recreated group it never deletes — the recovery locked by key "
        "while the replay deletes by id"
    )
    async with db_session_maker() as verify:
        surviving = (
            await verify.execute(select(DeviceGroup.id).where(DeviceGroup.key == pair.static_key))
        ).scalar_one_or_none()
    assert surviving == recreated_id[0], "the replay must delete its own row and leave the recreated one alone"
