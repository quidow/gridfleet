"""Deleting a static group must never race a concurrent *first* ``member_of``
reference to it into a dangling reference.

The sibling file covers the same collision on ``create_group``; this one covers
``update_group``, where the referring dynamic group already exists and survives
whichever writer loses. That difference is the whole reason both files exist:
here the dangling state is reachable without anything else being wrong, because
the dynamic row is committed before, during, and after the race.

Historical note, because it is why this file exists and why row locks are not
the answer. ``delete_group`` used to take ``FOR UPDATE`` over the target plus
every group whose ``filters['member_of']`` was already non-NULL. That closed the
deleter-first ordering only. Updater-first escaped it: the updater does not
write the dynamic row until commit, so the deleter's statement planned against a
snapshot in which that row still had a NULL ``member_of``, the predicate
filtered it out *before* ``LockRows``, and under READ COMMITTED EvalPlanQual
re-checks only rows the statement actually blocked on — never the excluded one.
Both transactions committed and the dynamic group was left pointing at a deleted
key.

What closes it now is neither a row lock nor an advisory lock but
``fk_device_group_member_of_static_group``. The reference is a row in
``device_group_member_of``, so inserting it takes ``FOR KEY SHARE`` on the
target the deleter must lock exclusively — a conflict PostgreSQL resolves,
against a set of rows no application predicate has to enumerate correctly.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, inspect, select

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services import groups as group_service
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import (
    assert_no_dangling_reference,
    build_groups_service,
    fetch_group_rows,
    fetch_member_of_keys,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# See the sibling module: widens the window, never decides the assertion.
HANDOFF_SEC = 0.5
EVENT_WAIT_TIMEOUT_SEC = 5.0
RACE_TIMEOUT_SEC = 15.0


async def _seed_unreferenced_pair(db_session: AsyncSession) -> tuple[str, str, uuid.UUID]:
    """A static group with *no* referrers and a dynamic group with no ``member_of``.

    Precisely the state the race starts from: the dynamic group is invisible to
    ``delete_group``'s dependent lookup, which joins ``device_group_member_of``
    and finds no row at all, until the concurrent update commits one.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    db_session.add(static)
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"device_type": "real_device"},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key, static.id


async def _wait(flag: asyncio.Event, *, label: str) -> None:
    try:
        await asyncio.wait_for(flag.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(f"{label}: the coordinating seam never fired within {EVENT_WAIT_TIMEOUT_SEC}s")


def _signal_once_the_edge_is_staged(session: AsyncSession, static_id: uuid.UUID, staged: asyncio.Event) -> None:
    """Set *staged* on the first ``execute`` that finds the edge row written.

    Wrapped around ``session.execute`` rather than ``session.flush``: the edge
    INSERT is a Core statement the writer issues directly, so the row exists in
    this transaction as soon as that call returns — no flush is involved. The
    probe reads inside the writer's own transaction, so it sees the uncommitted
    row a peer cannot. Holds afterwards so the released peer reaches its
    ``DELETE`` and blocks there instead of running after the commit.
    """
    original_execute = session.execute
    fired = False
    stmt = select(func.count()).select_from(DeviceGroupMemberOf).where(DeviceGroupMemberOf.static_group_id == static_id)

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(*args, **kwargs)
        if not fired and int((await original_execute(stmt)).scalar_one()):
            fired = True
            staged.set()
            await asyncio.sleep(HANDOFF_SEC)
        return result

    session.execute = _intercepted  # type: ignore[assignment, method-assign]


async def _assert_consistent_end_state(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    static_key: str,
    dynamic_key: str,
    update_result: object,
    delete_result: object,
) -> None:
    """The two-state invariant on the update path, plus the obliged outcome.

    The dynamic row exists in every branch here, so ``assert_no_dangling_reference``
    is meaningful rather than vacuous and is asserted alongside the end state.
    """
    await assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    references = await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key)
    static_exists = static_row is not None
    relation_exists = static_key in references

    assert dynamic_row is not None, "the referring dynamic group must survive either outcome"
    assert not (static_exists is False and relation_exists is True), (
        f"{dynamic_key} references deleted static group {static_key}"
    )
    assert (static_exists, relation_exists) in {(True, True), (False, False)}, (
        f"inconsistent end state: static_exists={static_exists} relation_exists={relation_exists}"
    )

    if relation_exists:
        assert not isinstance(update_result, Exception), f"the edge landed, so the update won: {update_result!r}"
        assert isinstance(delete_result, GroupReferencedError), (
            f"deleter must be refused once the edge exists, got {delete_result!r}"
        )
        assert delete_result.dependents == [dynamic_key], (
            f"the 409 must name the referrer, got {delete_result.dependents!r}"
        )
    else:
        assert delete_result is True, f"no edge landed, so the delete must have succeeded: {delete_result!r}"
        assert isinstance(update_result, UnknownMemberOfError), (
            f"updater must be refused once its target is gone, got {update_result!r}"
        )
        assert update_result.keys == [static_key], f"the 422 must name the missing target, got {update_result.keys!r}"


async def test_edge_committed_first_refuses_the_delete(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The updater stages the first reference; the deleter must then be refused."""
    static_key, dynamic_key, static_id = await _seed_unreferenced_pair(db_session)
    service = build_groups_service()
    staged = asyncio.Event()

    async def add_first_reference() -> dict[str, Any] | None:
        async with db_session_maker() as session:
            _signal_once_the_edge_is_staged(session, static_id, staged)
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        await _wait(staged, label="deleter")
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    update_result, delete_result = await asyncio.wait_for(
        asyncio.gather(add_first_reference(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        update_result=update_result,
        delete_result=delete_result,
    )
    assert await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key) == [static_key], (
        "the updater committed first, so its reference must have landed"
    )


async def test_target_deleted_after_resolution_refuses_the_update(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The updater resolves a live target and loses it before writing the edge.

    The resolve is no longer the authority — it succeeded against a row a peer
    then deleted and committed. The foreign key refuses the INSERT, and the
    service must surface the same ``UnknownMemberOfError`` a failed resolve
    raises rather than an ``IntegrityError`` the router would map to 500.
    """
    static_key, dynamic_key, _static_id = await _seed_unreferenced_pair(db_session)
    service = build_groups_service()
    resolved = asyncio.Event()
    deleted = asyncio.Event()
    original_resolve = group_service._resolve_static_member_of

    async def resolve_then_wait(db: AsyncSession, keys: set[str]) -> dict[str, DeviceGroup]:
        result = await original_resolve(db, keys)
        resolved.set()
        await _wait(deleted, label="updater")
        return result

    monkeypatch.setattr(group_service, "_resolve_static_member_of", resolve_then_wait)

    async def add_first_reference() -> dict[str, Any] | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        await _wait(resolved, label="deleter")
        try:
            async with db_session_maker() as session:
                return await service.delete_group(session, static_key)
        finally:
            deleted.set()

    update_result, delete_result = await asyncio.wait_for(
        asyncio.gather(add_first_reference(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    assert delete_result is True, f"the target was unreferenced when the delete ran: {delete_result!r}"
    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        update_result=update_result,
        delete_result=delete_result,
    )


async def test_reference_insert_violation_rolls_back_only_to_a_savepoint(db_session: AsyncSession) -> None:
    """A refused reference INSERT must not take its caller's transaction with it.

    ``_replace_member_of`` used to answer the foreign-key violation with a root
    ``db.rollback()`` — silently discarding whatever the caller had staged, from
    inside a helper the caller cannot see into. ``update_group`` arrives here
    with a flushed field update and a queued event.

    Still reachable, and deliberately so: the target ``FOR KEY SHARE`` this
    function hoists ahead of its edge ``DELETE`` orders the acquisition but does
    not verify it, so a target deleted between ``_resolve_static_member_of`` and
    the INSERT is caught by the foreign key rather than by a second application
    check. This test uses a target row that never existed at all, which raises
    the same violation without needing a peer.
    """
    suffix = uuid.uuid4().hex[:8]
    dynamic_key = f"dynamic-{suffix}"
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add(dynamic)
    await db_session.commit()
    dynamic_id = dynamic.id

    ghost = DeviceGroup(
        id=uuid.uuid4(),
        key=f"ghost-{uuid.uuid4().hex[:8]}",
        name="ghost",
        group_type=GroupType.static,
    )

    with pytest.raises(UnknownMemberOfError) as exc:
        await group_service._replace_member_of(db_session, dynamic_id, {ghost.key: ghost})

    assert exc.value.keys == [ghost.key]
    assert db_session.in_transaction(), (
        "a rejected reference INSERT rolled the caller's transaction back to the root, not to a SAVEPOINT"
    )
    assert not inspect(dynamic).expired, "a savepoint rollback must leave a clean loaded row populated"
    assert dynamic.key == dynamic_key
    await db_session.rollback()


async def test_unsynchronised_update_and_delete_reach_a_consistent_state(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """No seam: whichever writer wins, the pair must still agree."""
    static_key, dynamic_key, _static_id = await _seed_unreferenced_pair(db_session)
    service = build_groups_service()

    async def add_first_reference() -> dict[str, Any] | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    update_result, delete_result = await asyncio.wait_for(
        asyncio.gather(add_first_reference(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        update_result=update_result,
        delete_result=delete_result,
    )
