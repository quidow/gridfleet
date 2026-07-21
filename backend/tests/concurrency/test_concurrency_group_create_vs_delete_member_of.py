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
from typing import TYPE_CHECKING

import pytest

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupCreate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import (
    build_groups_service,
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

    async def create_dynamic() -> DeviceGroup:
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
        await wait_for_group_lock(creator_locked, label="creator")
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

    async def create_dynamic() -> DeviceGroup:
        await wait_for_group_lock(deleter_locked, label="deleter")
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
