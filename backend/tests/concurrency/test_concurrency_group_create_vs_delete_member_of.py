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
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services import groups as group_service
from app.devices.services.groups import (
    GroupReferencedError,
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
# specific seam sets; if a seam stops firing (renamed function, changed flush
# count) an unbounded wait would hang the run, because ``pytest-timeout`` is
# deliberately not a dependency here.
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


async def _relation_count(session: AsyncSession, static_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(DeviceGroupMemberOf).where(DeviceGroupMemberOf.static_group_id == static_id)
    return int((await session.execute(stmt)).scalar_one())


def _signal_once_the_edge_is_staged(session: AsyncSession, session_id: uuid.UUID, staged: asyncio.Event) -> None:
    """Set *staged* on the first ``flush`` that finds the edge row written.

    Wrapped around ``session.flush``, not around SQL text: the seam is "the
    relation row is in this transaction and its ``FOR KEY SHARE`` on the target
    is held", which is a state the session can be asked about directly. The
    probe reads inside the writer's own transaction, so it sees the uncommitted
    row a peer cannot.

    Holds for ``HANDOFF_SEC`` afterwards so the released peer reaches its own
    ``DELETE`` and blocks there rather than running after the commit.
    """
    original_flush = session.flush
    fired = False

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_flush(*args, **kwargs)
        if not fired and await _relation_count(session, session_id):
            fired = True
            staged.set()
            await asyncio.sleep(HANDOFF_SEC)
        return result

    session.flush = _intercepted  # type: ignore[assignment, method-assign]


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

    async def create_dynamic() -> dict[str, Any]:
        async with db_session_maker() as session:
            _signal_once_the_edge_is_staged(session, static_id, staged)
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
                return await service.delete_group(session, static_key)
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


async def test_blind_dependent_scan_still_yields_a_named_409(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correctness must not depend on the preflight scan seeing anything.

    ``_dependent_dynamic_keys`` is an optimisation and an error-message source,
    not the guard. Blind its first call — the state a peer's mid-flight commit
    produces — and the delete must still be refused, and still name the
    referrer, because the refusal comes from the foreign key.
    """
    static_key, dynamic_key, static_id = await _seed_static(db_session)
    dynamic = DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic)
    db_session.add(dynamic)
    await db_session.flush()
    db_session.add(DeviceGroupMemberOf(dynamic_group_id=dynamic.id, static_group_id=static_id))
    await db_session.commit()

    original = group_service._dependent_dynamic_keys
    calls = 0

    async def blind_first_scan(db: AsyncSession, group_id: uuid.UUID) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        return await original(db, group_id)

    monkeypatch.setattr(group_service, "_dependent_dynamic_keys", blind_first_scan)

    async with db_session_maker() as session:
        with pytest.raises(GroupReferencedError) as exc:
            await build_groups_service().delete_group(session, static_key)

    assert exc.value.dependents == [dynamic_key]
    assert calls >= 2, "the blinded preflight must have been followed by a re-read"
    static_row, _ = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "a refused delete must leave the target in place"


async def test_rejected_writers_leave_no_open_transaction(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every rejecting or empty-handed group write must end its own transaction.

    Each mutator's first statement autobegins one. A writer that returns
    ``None``/``False`` or raises a typed rejection without committing leaves it
    open until the session closes — for an API request, until after response
    serialization, where ``idle_in_transaction_session_timeout`` is the only
    thing that ends it. The sibling race tests all call the service inside
    ``async with db_session_maker() as session``, whose exit rolls back and
    would hide this; these assert *while the session is still open*.
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
        assert not session.in_transaction(), "a refused delete_group left its transaction open"

        assert await service.delete_group(session, missing) is False
        assert not session.in_transaction(), "delete_group on an unknown key left its read transaction open"

        assert await service.update_group(session, missing, DeviceGroupUpdate(description="x")) is None
        assert not session.in_transaction(), "update_group on an unknown key left its read transaction open"

        with pytest.raises(UnknownMemberOfError):
            await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [missing]}),  # type: ignore[arg-type]
            )
        assert not session.in_transaction(), "a refused update_group left its transaction open"

        with pytest.raises(UnknownMemberOfError):
            await service.create_group(
                session,
                DeviceGroupCreate(
                    key=f"other-{uuid.uuid4().hex[:8]}",
                    name="other",
                    group_type=GroupType.dynamic,
                    filters={"member_of": [missing]},  # type: ignore[arg-type]
                ),
            )
        assert not session.in_transaction(), "a refused create_group left its transaction open"


async def test_capture_statements_does_not_open_a_transaction(db_session: AsyncSession) -> None:
    assert not db_session.in_transaction()

    async with capture_statements(db_session):
        assert not db_session.in_transaction()
