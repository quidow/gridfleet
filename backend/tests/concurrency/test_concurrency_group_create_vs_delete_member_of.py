"""Creating a dynamic group must never race a concurrent delete of the static
group it references into a dangling ``member_of``.

Nothing in the application serialises these two writers any more. The authority
is ``device_group_member_of``'s restrictive target foreign key
(``fk_device_group_member_of_static_group``, ``ON DELETE RESTRICT``): the
creator's INSERT takes ``FOR KEY SHARE`` on the static row it names, and the
deleter's ``DELETE`` needs a conflicting lock on that same row, so one of them
is refused by PostgreSQL rather than by a process-local scan.

Every test here asserts durable database state, not timing. Exactly two end
states are consistent:

* the edge exists **and** its target exists — the deleter must have been
  refused with ``GroupReferencedError``;
* neither exists — the creator must have been refused with
  ``UnknownMemberOfError`` and left no dynamic group behind.

The third combination (edge without target) is the bug this phase exists to
make unrepresentable, so it is asserted against explicitly rather than left to
fall out of the first check.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services import groups as group_service
from app.devices.services.groups import (
    GroupReferencedError,
    GroupWriteResult,
    UnknownMemberOfError,
    constraint_name,
)
from tests.concurrency.group_lock_helpers import (
    build_groups_service,
    capture_statements,
    fetch_group_rows,
    fetch_member_of_keys,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Widens the window between the two writers so the interesting interleaving is
# the common one. Only ever affects *which* of the two consistent end states is
# reached — both are asserted, so no test outcome depends on this value.
HANDOFF_SEC = 0.5

# A bound, not a race parameter. Every wait in this module is on an event a
# specific seam sets; if a seam stops firing (renamed function, changed
# statement shape) an unbounded wait would hang the run, because
# ``pytest-timeout`` is deliberately not a dependency here.
EVENT_WAIT_TIMEOUT_SEC = 5.0

# Ceiling for a whole two-writer gather. Generous relative to HANDOFF_SEC: it
# turns a wedged interleaving into a legible failure instead of a hung job.
RACE_TIMEOUT_SEC = 15.0


async def _seed_static(db_session: AsyncSession) -> tuple[str, str, uuid.UUID]:
    """A lone static group and an unused key for the dynamic group to be created."""
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    static = DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)
    db_session.add(static)
    await db_session.commit()
    return static_key, dynamic_key, static.id


async def _wait(flag: asyncio.Event, *, label: str) -> None:
    try:
        await asyncio.wait_for(flag.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(f"{label}: the coordinating seam never fired within {EVENT_WAIT_TIMEOUT_SEC}s")


def _signal_once_the_edge_is_staged(session: AsyncSession, session_id: uuid.UUID, staged: asyncio.Event) -> None:
    """Set *staged* on the first ``execute`` that finds the edge row written.

    Wrapped around ``session.execute``, not ``session.flush``: the edge INSERT
    is a Core statement the writer issues directly, so the row exists in this
    transaction as soon as that call returns — no flush is involved. The probe
    reads inside the writer's own transaction, so it sees the uncommitted row a
    peer cannot.

    Holds for ``HANDOFF_SEC`` afterwards so the released peer reaches its own
    ``DELETE`` and blocks there rather than running after the commit.
    """
    original_execute = session.execute
    fired = False
    count_stmt = (
        select(func.count()).select_from(DeviceGroupMemberOf).where(DeviceGroupMemberOf.static_group_id == session_id)
    )

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(*args, **kwargs)
        if not fired and int((await original_execute(count_stmt)).scalar_one()):
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
    create_result: object,
    delete_result: object,
) -> None:
    """The two-state invariant, plus the outcome each state obliges."""
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    references = await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key)
    static_exists = static_row is not None
    relation_exists = static_key in references

    assert not (static_exists is False and relation_exists is True), (
        f"{dynamic_key} references deleted static group {static_key}"
    )
    assert (static_exists, relation_exists) in {(True, True), (False, False)}, (
        f"inconsistent end state: static_exists={static_exists} relation_exists={relation_exists}"
    )

    if relation_exists:
        assert not isinstance(create_result, Exception), f"the edge landed, so the create won: {create_result!r}"
        assert isinstance(delete_result, GroupReferencedError), (
            f"deleter must be refused once the edge exists, got {delete_result!r}"
        )
        assert delete_result.dependents == [dynamic_key], (
            f"the 409 must name the referrer, got {delete_result.dependents!r}"
        )
        assert dynamic_row is not None, "the referring dynamic group must exist"
    else:
        assert delete_result is True, f"no edge landed, so the delete must have succeeded: {delete_result!r}"
        assert isinstance(create_result, UnknownMemberOfError), (
            f"creator must be refused once its target is gone, got {create_result!r}"
        )
        assert create_result.keys == [static_key], f"the 422 must name the missing target, got {create_result.keys!r}"
        assert dynamic_row is None, "a refused create must not leave its dynamic group behind"


async def test_edge_committed_first_refuses_the_delete(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The creator stages its edge, then the deleter runs against a live target.

    The deleter's ``DELETE`` blocks on the ``FOR KEY SHARE`` the pending INSERT
    holds, so it can only proceed once the creator has committed — at which
    point the restrictive foreign key refuses it.
    """
    static_key, dynamic_key, static_id = await _seed_static(db_session)
    service = build_groups_service()
    staged = asyncio.Event()

    async def create_dynamic() -> GroupWriteResult:
        async with db_session_maker() as session:
            _signal_once_the_edge_is_staged(session, static_id, staged)
            result = await service.create_group(
                session,
                DeviceGroupCreate(
                    key=dynamic_key,
                    name=dynamic_key,
                    group_type=GroupType.dynamic,
                    filters={"member_of": [static_key]},  # type: ignore[arg-type]
                ),
            )
            # create_group no longer self-commits (Phase 11): commit here, the
            # way the router's ``session_factory.begin()`` now does, so the
            # edge actually lands before this coroutine returns. Without this
            # the insert would be rolled back at the ``async with`` exit and
            # the interleaving below could never be reached.
            await session.commit()
            return result

    async def delete_static() -> bool:
        await _wait(staged, label="deleter")
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    create_result, delete_result = await asyncio.wait_for(
        asyncio.gather(create_dynamic(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        create_result=create_result,
        delete_result=delete_result,
    )
    static_row, _ = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "the blocked delete must have been refused, not applied"


async def test_target_deleted_after_resolution_refuses_the_create(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The creator resolves a live target, then loses it before inserting.

    This is the interleaving the advisory lock used to exclude outright, and the
    reason the resolve is no longer the authority: it succeeded against a row
    that a peer deleted and committed before the edge was written. Only the
    foreign key can refuse the INSERT at that point, and the service has to
    translate it into the same ``UnknownMemberOfError`` a failed resolve raises.
    """
    static_key, dynamic_key, _static_id = await _seed_static(db_session)
    service = build_groups_service()
    resolved = asyncio.Event()
    deleted = asyncio.Event()
    original_resolve = group_service._resolve_static_member_of

    async def resolve_then_wait(db: AsyncSession, keys: set[str]) -> dict[str, DeviceGroup]:
        result = await original_resolve(db, keys)
        resolved.set()
        await _wait(deleted, label="creator")
        return result

    monkeypatch.setattr(group_service, "_resolve_static_member_of", resolve_then_wait)

    async def create_dynamic() -> dict[str, Any]:
        async with db_session_maker() as session:
            return await service.create_group(
                session,
                DeviceGroupCreate(
                    key=dynamic_key,
                    name=dynamic_key,
                    group_type=GroupType.dynamic,
                    filters={"member_of": [static_key]},  # type: ignore[arg-type]
                ),
            )

    async def delete_static() -> bool:
        await _wait(resolved, label="deleter")
        try:
            async with db_session_maker() as session:
                result = await service.delete_group(session, static_key)
                # delete_group no longer self-commits (Phase 11): commit here,
                # the way the router's session_factory.begin() now does, so a
                # successful delete is actually durable before the creator's
                # blocked INSERT resumes against it.
                await session.commit()
                return result
        finally:
            deleted.set()

    create_result, delete_result = await asyncio.wait_for(
        asyncio.gather(create_dynamic(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    assert delete_result is True, f"the target was unreferenced when the delete ran: {delete_result!r}"
    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        create_result=create_result,
        delete_result=delete_result,
    )


async def test_unsynchronised_create_and_delete_reach_a_consistent_state(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """No seam at all: whichever writer wins, the pair must agree.

    The coordinated tests above each pin one branch. This one pins that there is
    no *third* branch, on an interleaving nothing in the test steers.
    """
    static_key, dynamic_key, _static_id = await _seed_static(db_session)
    service = build_groups_service()

    async def create_dynamic() -> GroupWriteResult:
        async with db_session_maker() as session:
            result = await service.create_group(
                session,
                DeviceGroupCreate(
                    key=dynamic_key,
                    name=dynamic_key,
                    group_type=GroupType.dynamic,
                    filters={"member_of": [static_key]},  # type: ignore[arg-type]
                ),
            )
            # create_group no longer self-commits (Phase 11): commit here, the
            # way the router's ``session_factory.begin()`` now does. Without
            # it the ``(True, True)`` end state (the edge landed and stuck) is
            # unreachable — the insert would always be rolled back at the
            # ``async with`` exit — and a creator that wins the lock race
            # would report a successful ``GroupWriteResult`` for a group that
            # was never actually committed, which is not one of the two
            # consistent end states this test asserts.
            await session.commit()
            return result

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            result = await service.delete_group(session, static_key)
            # delete_group no longer self-commits (Phase 11): commit here, the
            # way the router's session_factory.begin() now does. Without it,
            # the (True, True) end state — the edge landed and stuck — is
            # unreachable on the delete side too: a delete that "won" the race
            # would roll back at the async with exit and report success for a
            # group that was never actually removed.
            await session.commit()
            return result

    create_result, delete_result = await asyncio.wait_for(
        asyncio.gather(create_dynamic(), delete_static(), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )

    await _assert_consistent_end_state(
        db_session_maker,
        static_key=static_key,
        dynamic_key=dynamic_key,
        create_result=create_result,
        delete_result=delete_result,
    )


async def test_restrictive_foreign_key_refuses_a_plain_delete(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """No service involved: the database itself refuses the referenced target.

    This is the floor everything else stands on. ``DELETE FROM device_groups``
    issued directly — no dependent scan, no application code — must raise
    ``fk_device_group_member_of_static_group``.
    """
    static_key, dynamic_key, static_id = await _seed_static(db_session)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add(dynamic)
    await db_session.flush()
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=dynamic.id, static_group_id=static_id))
    await db_session.commit()

    async with db_session_maker() as raw:
        with pytest.raises(IntegrityError) as exc:
            await raw.execute(delete(DeviceGroup).where(DeviceGroup.id == static_id))
        assert constraint_name(exc.value) == "fk_device_group_member_of_static_group"
        await raw.rollback()

    static_row, _ = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "the refused DELETE must have left the target in place"


async def test_rejected_writers_leave_no_open_transaction(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every rejecting or empty-handed group write must leave its transaction for the caller to close.

    ``delete_group`` and ``update_group`` (Phase 11) no longer self-clean, the
    same way ``create_group`` stopped in the sibling task: a writer that
    returns ``None``/``False`` or raises a typed rejection leaves its
    autobegun transaction open, deferring to the caller's boundary the way its
    success path already does. On a hand-started session with no wrapping
    ``begin()`` — the shape every call below uses — that means the
    transaction stays **open** across all four calls, not closed; this flips
    to ``False`` if either writer's ``finally: rollback`` is reintroduced. The
    sibling race tests all call the service inside
    ``async with db_session_maker() as session``, whose exit rolls back and
    would hide this; these assert *while the session is still open*.

    ``create_group`` pins the same property one level down below: on a
    rejection, nothing has unwound its transaction either, and only the
    caller's own explicit rollback discards the partial work.
    """
    static_key, dynamic_key, static_id = await _seed_static(db_session)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add(dynamic)
    await db_session.flush()
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=dynamic.id, static_group_id=static_id))
    await db_session.commit()
    service = build_groups_service()
    missing = f"missing-{uuid.uuid4().hex[:8]}"

    async with db_session_maker() as session:
        with pytest.raises(GroupReferencedError):
            await service.delete_group(session, static_key)
        assert session.in_transaction(), (
            "a refused delete_group must leave its transaction open for the caller's boundary to unwind"
        )

        assert await service.delete_group(session, missing) is False
        assert session.in_transaction(), (
            "delete_group on an unknown key must leave its read transaction open for the caller to close"
        )

        assert await service.update_group(session, missing, DeviceGroupUpdate(description="x")) is None
        assert session.in_transaction(), (
            "update_group on an unknown key must leave its read transaction open for the caller to close"
        )

        with pytest.raises(UnknownMemberOfError):
            await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [missing]}),  # type: ignore[arg-type]
            )
        assert session.in_transaction(), (
            "a refused update_group must leave its transaction open for the caller's boundary to unwind"
        )

    other_key = f"other-{uuid.uuid4().hex[:8]}"
    async with db_session_maker() as session:
        with pytest.raises(UnknownMemberOfError):
            await service.create_group(
                session,
                DeviceGroupCreate(
                    key=other_key,
                    name="other",
                    group_type=GroupType.dynamic,
                    filters={"member_of": [missing]},  # type: ignore[arg-type]
                ),
            )
        # The new contract, asserted where it is actually falsifiable: nothing
        # has unwound this transaction yet, because create_group no longer
        # rolls back its own rejections. A caller with no boundary of its own
        # (this hand-started session) is left holding it open.
        #
        # No durability check follows, deliberately. This rejection is raised by
        # _resolve_static_member_of, before _insert_group stages anything, so
        # there is no partial row for a rollback to discard and a count here
        # would read 0 whatever the boundary did. The property "the caller's
        # boundary takes the partial work down" needs a failure *after* the
        # INSERT and is pinned through the real router boundary by
        # tests/devices/test_group_command_boundaries.py::
        # test_create_group_failure_leaves_no_row_and_no_edge.
        assert session.in_transaction(), (
            "a refused create_group must leave its transaction open for the caller's boundary to unwind"
        )


async def test_public_count_leaves_its_transaction_for_the_caller_to_close(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Renamed from ``..._leaves_no_open_transaction`` (Phase 11): that name
    stopped being true of ``dynamic_device_count`` itself. It has no
    ``finally: rollback`` — its first ``SELECT`` autobegins a transaction it
    never ends, on purpose, because it now runs on a throwaway session the
    *caller* opens and closes (``_with_dynamic_count`` in the router; a bare
    ``async with db_session_maker()`` here). Checking ``in_transaction()``
    only after that session's ``async with`` exits would just restate
    ``AsyncSession.close()``'s own guarantee, not anything this module wrote —
    so the transaction's presence is asserted *while the session is still
    open*, which is exactly where it is falsifiable: a ``finally: rollback``
    added back to ``dynamic_device_count`` would flip this to ``False``.

    The create half pins the mirror property for ``create_group``'s success
    path: it must not commit early either, deferring to the caller's
    ``begin()`` block the same way its rejections do (see
    ``test_rejected_writers_leave_no_open_transaction``).

    ``update_group`` (Phase 11) no longer computes a device count of its own
    at all — the router folds it in afterwards through this same
    ``dynamic_device_count`` — so its assertion below is re-pointed the same
    way: the transaction it autobegins stays open for the caller to close,
    where it used to be checked closed.
    """
    static_key, dynamic_key, _static_id = await _seed_static(db_session)
    service = build_groups_service()

    async with db_session_maker.begin() as command_db:
        created = await service.create_group(
            command_db,
            DeviceGroupCreate(
                key=dynamic_key,
                name=dynamic_key,
                group_type=GroupType.dynamic,
                filters={"member_of": [static_key]},  # type: ignore[arg-type]
            ),
        )
        assert command_db.in_transaction(), (
            "create_group must not commit its own success early — that is the caller's begin() block's job"
        )

    async with db_session_maker() as count_db:
        device_count = await service.dynamic_device_count(
            count_db, group_id=created.group_id, group_key=created.group_key
        )
        assert count_db.in_transaction(), (
            "dynamic_device_count must leave its reads' transaction open for the caller's session close to end"
        )
    payload = dict(created.payload)
    payload["device_count"] = device_count
    assert payload["device_count"] == 0

    async with db_session_maker() as session:
        updated = await service.update_group(session, dynamic_key, DeviceGroupUpdate(description="relabelled"))
        assert updated is not None
        assert session.in_transaction(), (
            "update_group must leave its transaction open for the caller's boundary to close"
        )

    # The failure branch matters more than the success one: it returns ``None``
    # from an ``except`` clause, so the ``finally`` is the only thing that can
    # end the transaction the reads before the failure opened. The failure is
    # real, not injected — ``filters`` written straight to the column with a
    # value ``DeviceGroupFilters`` rejects, which is what a hand-edited row or a
    # rolled-back schema change looks like, and which raises inside
    # ``_load_devices_in_scope`` after ``load_member_of_keys`` has already read.
    async with db_session_maker() as poisoned:
        await poisoned.execute(
            update(DeviceGroup).where(DeviceGroup.key == dynamic_key).values(filters={"device_type": "not-a-type"})
        )
        await poisoned.commit()

    async with db_session_maker() as session:
        group = (await session.execute(select(DeviceGroup).where(DeviceGroup.key == dynamic_key))).scalar_one_or_none()
        assert group is not None
        # Commit, not rollback: both end the read transaction, but a root
        # rollback expires every loaded row and ``group`` is about to be read
        # attribute-by-attribute from a synchronous context.
        await session.commit()
        count = await service.dynamic_device_count(session, group_id=group.id, group_key=group.key)
        assert count is None, "the poisoned filters must have raised"
        assert session.in_transaction(), (
            "a failed dynamic device count must still leave its reads open for the caller to close"
        )


async def test_capture_statements_does_not_open_a_transaction(db_session: AsyncSession) -> None:
    assert not db_session.in_transaction()

    async with capture_statements(db_session):
        assert not db_session.in_transaction()
