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
from sqlalchemy import select

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupCreate
from app.devices.services.groups import (
    DeviceGroupsService,
    GroupReferencedError,
    UnknownMemberOfError,
)
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Long enough for the peer's blocked advisory-lock acquire to reach the lock
# manager before the holder commits. Only widens the race window.
_HANDOFF_SEC = 0.5


def _service() -> DeviceGroupsService:
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
    )


async def _seed_static(db_session: AsyncSession) -> tuple[str, str]:
    """A lone static group and an unused key for the dynamic group to be created."""
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    await db_session.commit()
    return static_key, dynamic_key


def _signal_after_group_lock(session: AsyncSession, locked: asyncio.Event) -> None:
    """Set *locked* once *session* holds the group-mutation advisory lock.

    Then hold inside the interception for ``_HANDOFF_SEC`` so the peer
    transaction reaches its own ``pg_advisory_xact_lock`` and blocks there.
    """
    original_execute = session.execute
    fired = False

    async def _intercepted(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(stmt, *args, **kwargs)
        if not fired and "pg_advisory_xact_lock" in str(stmt).lower():
            fired = True
            locked.set()
            await asyncio.sleep(_HANDOFF_SEC)
        return result

    session.execute = _intercepted  # type: ignore[assignment, method-assign]


async def _assert_no_dangling_reference(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    static_key: str,
    dynamic_key: str,
) -> None:
    async with db_session_maker() as verify:
        static_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == static_key))
        ).scalar_one_or_none()
        dynamic_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == dynamic_key))
        ).scalar_one_or_none()
        if static_row is None and dynamic_row is not None:
            member_of = (dynamic_row.filters or {}).get("member_of", [])
            assert static_key not in member_of, (
                f"dynamic group {dynamic_key} references deleted static group {static_key}"
            )


async def test_create_wins_delete_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The creator serialises first; the deleter must then observe the new reference."""
    static_key, dynamic_key = await _seed_static(db_session)
    service = _service()
    creator_locked = asyncio.Event()

    async def create_dynamic() -> DeviceGroup:
        async with db_session_maker() as session:
            _signal_after_group_lock(session, creator_locked)
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
        await creator_locked.wait()
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    create_result, delete_result = await asyncio.gather(create_dynamic(), delete_static(), return_exceptions=True)

    assert not isinstance(create_result, Exception), f"creator should have won: {create_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the committed reference, got {delete_result!r}"
    )
    await _assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)


async def test_delete_wins_create_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The deleter serialises first; the creator must then find its target gone."""
    static_key, dynamic_key = await _seed_static(db_session)
    service = _service()
    deleter_locked = asyncio.Event()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            _signal_after_group_lock(session, deleter_locked)
            return await service.delete_group(session, static_key)

    async def create_dynamic() -> DeviceGroup:
        await deleter_locked.wait()
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
    await _assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
