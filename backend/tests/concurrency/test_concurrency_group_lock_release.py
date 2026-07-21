"""Every rejected group write must release the advisory lock immediately.

``acquire_group_mutation_lock`` is transaction-scoped, so a writer that returns
or raises without committing or rolling back keeps a *fleet-global* lock until
the session closes — for an API request, that is after response serialization.
A client hammering PATCH/DELETE on unknown keys would then serialise every
group write in the system for the tail of each request.

These tests assert the release *while the rejecting session is still open*.
That distinction is the whole point: the sibling concurrency tests call the
service inside ``async with db_session_maker() as session``, and that context
manager's exit rolls back — silently releasing a lock the service should have
released itself, and hiding exactly this defect.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest

from app.core.locks import acquire_group_mutation_lock
from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import build_groups_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Generous: a released lock is acquired in microseconds, so any real wait here
# means the lock is still held. Only bounds the failure, never the pass.
_ACQUIRE_TIMEOUT_SEC = 5.0


async def _assert_lock_is_free(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    after: str,
) -> None:
    """Fail unless a fresh transaction can take the group-mutation lock now."""
    async with db_session_maker() as peer:
        try:
            await asyncio.wait_for(acquire_group_mutation_lock(peer), timeout=_ACQUIRE_TIMEOUT_SEC)
        except TimeoutError:
            pytest.fail(
                f"the group-mutation advisory lock was still held after {after}. "
                "That writer returned without commit or rollback, so a fleet-global "
                "lock survives until the session closes at request teardown."
            )
        await peer.rollback()


async def _seed_referenced_pair(db_session: AsyncSession) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"member_of": [static_key]},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key


async def test_delete_of_referenced_group_releases_the_lock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The 409 path: _assert_no_references raises after the lock is taken."""
    static_key, _dynamic_key = await _seed_referenced_pair(db_session)
    service = build_groups_service()

    async with db_session_maker() as session:
        with pytest.raises(GroupReferencedError):
            await service.delete_group(session, static_key)
        await _assert_lock_is_free(db_session_maker, after="a rejected delete_group (GroupReferencedError)")


async def test_delete_of_unknown_group_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    async with db_session_maker() as session:
        assert await service.delete_group(session, f"missing-{uuid.uuid4().hex[:8]}") is False
        await _assert_lock_is_free(db_session_maker, after="delete_group on an unknown key")


async def test_update_of_unknown_group_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    async with db_session_maker() as session:
        result = await service.update_group(
            session,
            f"missing-{uuid.uuid4().hex[:8]}",
            DeviceGroupUpdate(description="never applied"),
        )
        assert result is None
        await _assert_lock_is_free(db_session_maker, after="update_group on an unknown key")


async def test_update_with_unresolvable_member_of_releases_the_lock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _static_key, dynamic_key = await _seed_referenced_pair(db_session)
    service = build_groups_service()

    async with db_session_maker() as session:
        with pytest.raises(UnknownMemberOfError):
            await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [f"nope-{uuid.uuid4().hex[:8]}"]}),  # type: ignore[arg-type]
            )
        await _assert_lock_is_free(db_session_maker, after="update_group with an unresolvable member_of")


async def test_create_with_unresolvable_member_of_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    suffix = uuid.uuid4().hex[:8]

    async with db_session_maker() as session:
        with pytest.raises(UnknownMemberOfError):
            await service.create_group(
                session,
                DeviceGroupCreate(
                    key=f"dynamic-{suffix}",
                    name=f"dynamic-{suffix}",
                    group_type=GroupType.dynamic,
                    filters={"member_of": [f"nope-{suffix}"]},  # type: ignore[arg-type]
                ),
            )
        await _assert_lock_is_free(db_session_maker, after="create_group with an unresolvable member_of")
