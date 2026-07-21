"""Creating a dynamic group must never race a concurrent delete of the static
group it references into a dangling ``member_of``.

``FOR UPDATE`` cannot close this: the deleter's statement takes its snapshot
before the creator's INSERT exists, so there is no row to lock and
``_assert_no_references`` finds nothing. Only serialising the two writers works.

Both interleavings must end in a typed rejection, never in a committed dynamic
group pointing at a deleted key.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core.locks import acquire_group_mutation_lock
from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import (
    build_groups_service,
    capture_statements,
    fetch_group_rows,
    signal_after_group_lock,
    wait_for_group_lock,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _seed_static(db_session: AsyncSession) -> tuple[str, str]:
    """A lone static group and an unused key for the dynamic group to be created."""
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    await db_session.commit()
    return static_key, dynamic_key


async def test_create_wins_delete_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The creator serialises first; the deleter must then observe the new reference."""
    static_key, dynamic_key = await _seed_static(db_session)
    service = build_groups_service()
    creator_locked = asyncio.Event()

    async def create_dynamic() -> dict[str, Any]:
        async with db_session_maker() as session:
            signal_after_group_lock(session, creator_locked)
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
        await wait_for_group_lock(creator_locked, label="deleter")
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    create_result, delete_result = await asyncio.gather(create_dynamic(), delete_static(), return_exceptions=True)

    assert not isinstance(create_result, Exception), f"creator should have won: {create_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the committed reference, got {delete_result!r}"
    )

    # Pin the exact end state: the create won, so the static group must survive
    # the rejected delete and the dynamic group must exist referencing it.
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "static group must survive the rejected delete"
    assert dynamic_row is not None, "dynamic group must exist after the winning create"
    assert (dynamic_row.filters or {}).get("member_of") == [static_key], (
        f"dynamic group {dynamic_key} must reference the surviving static group {static_key}"
    )


async def test_delete_wins_create_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The deleter serialises first; the creator must then find its target gone."""
    static_key, dynamic_key = await _seed_static(db_session)
    service = build_groups_service()
    deleter_locked = asyncio.Event()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            signal_after_group_lock(session, deleter_locked)
            return await service.delete_group(session, static_key)

    async def create_dynamic() -> dict[str, Any]:
        await wait_for_group_lock(deleter_locked, label="creator")
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

    delete_result, create_result = await asyncio.gather(delete_static(), create_dynamic(), return_exceptions=True)

    assert delete_result is True, f"deleter should have won: {delete_result!r}"
    assert isinstance(create_result, UnknownMemberOfError), (
        f"creator must reject a reference to the just-deleted group, got {create_result!r}"
    )

    # Pin the exact end state: the delete won, so the static group must be gone
    # and the rejected create must never have left a dynamic row behind.
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is None, "static group must be gone after the winning delete"
    assert dynamic_row is None, "dynamic group must not exist after the rejected create"


def _assert_locked_before_group_reads(statements: list[str]) -> None:
    """Every ``device_groups`` read follows the advisory lock, and none takes a row lock.

    A narrow predicate row lock is the construct 00a87549 proved broken, and the
    whole-table one that replaced it is the N-2 scaling concern; under the
    advisory lock neither is needed. A read issued *before* the lock would carry
    a stale snapshot and defeat the serialisation, so ordering is asserted too.
    """
    assert any("pg_advisory_xact_lock" in s.lower() for s in statements), statements
    lock_index = next(i for i, s in enumerate(statements) if "pg_advisory_xact_lock" in s.lower())
    group_reads = [i for i, s in enumerate(statements) if "device_groups" in s.lower()]
    assert all(i > lock_index for i in group_reads), f"device_groups read before the advisory lock: {statements}"
    row_locks = [s for s in statements if "device_groups" in s.lower() and "for update" in s.lower()]
    assert not row_locks, f"device_groups row locks should be gone: {row_locks}"


async def test_delete_group_locks_before_reading_and_takes_no_row_lock(db_session: AsyncSession) -> None:
    static_key, _dynamic_key = await _seed_static(db_session)

    async with capture_statements(db_session) as statements:
        assert await build_groups_service().delete_group(db_session, static_key) is True

    _assert_locked_before_group_reads(statements)


async def test_update_group_locks_before_reading(db_session: AsyncSession) -> None:
    """Same ordering requirement on the update path."""
    static_key, dynamic_key = await _seed_static(db_session)
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"member_of": [static_key]},
        )
    )
    await db_session.commit()

    async with capture_statements(db_session) as statements:
        updated = await build_groups_service().update_group(
            db_session,
            dynamic_key,
            DeviceGroupUpdate(description="relabelled"),
        )
    assert updated is not None

    _assert_locked_before_group_reads(statements)


async def test_capture_statements_does_not_open_a_transaction(db_session: AsyncSession) -> None:
    assert not db_session.in_transaction()

    async with capture_statements(db_session):
        assert not db_session.in_transaction()


async def test_signal_after_group_lock_restores_session_execute(db_session: AsyncSession) -> None:
    locked = asyncio.Event()
    original_execute = db_session.execute
    signal_after_group_lock(db_session, locked)

    await acquire_group_mutation_lock(db_session)

    assert locked.is_set()
    assert db_session.execute == original_execute
    await db_session.rollback()
