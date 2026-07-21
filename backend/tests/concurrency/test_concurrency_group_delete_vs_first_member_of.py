"""Deleting a static group must never race a concurrent *first* ``member_of``
reference to it into a dangling reference.

Both orderings are closed by the group-mutation advisory lock
(``app/core/locks.py``): ``update_group`` and ``delete_group`` cannot overlap,
so the deleter's scan always runs against a snapshot carrying the updater's
committed reference, and an updater that loses the race re-reads a target that
is already gone.

Historical note, because it is why this file exists and why row locks are not
the answer here. ``delete_group`` used to take ``FOR UPDATE`` over the target
plus every group whose ``filters['member_of']`` was already non-NULL. That
closed the deleter-first ordering only. Updater-first escaped it: the updater
does not write the dynamic row until commit, so the deleter's statement planned
against a snapshot in which that row still had a NULL ``member_of``, the
predicate filtered it out *before* ``LockRows``, and under READ COMMITTED
EvalPlanQual re-checks only rows the statement actually blocked on — never the
excluded one. Both transactions committed and the dynamic group was left
pointing at a deleted key. Neither ``delete_group`` nor ``update_group`` takes
any ``device_groups`` row lock today; the shared helper this file imports
intercepts ``pg_advisory_xact_lock``, not ``FOR UPDATE``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from tests.concurrency.group_lock_helpers import (
    assert_no_dangling_reference,
    build_groups_service,
    fetch_group_rows,
    signal_after_group_lock,
    wait_for_group_lock,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _seed_unreferenced_pair(db_session: AsyncSession) -> tuple[str, str]:
    """A static group with *no* referrers and a dynamic group with no ``member_of``.

    This is precisely the state the review's interleaving starts from: the
    dynamic group is invisible to ``delete_group``'s referrer scan until the
    concurrent update commits.

    That scan is ``filters.has_key("member_of")`` (the ``member_of IS NOT NULL``
    form this file's module docstring describes is history). The seeded row
    carries ``filters={"device_type": ...}`` and no ``member_of`` key at all, so
    ``has_key`` does not match it — and a row seeded with an *empty*
    ``member_of`` would not match either, because ``_dump_filters`` pops the key
    rather than storing ``[]``.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"device_type": "real_device"},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key


async def test_delete_wins_first_member_of_reference_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The deleter serialises first; the updater then re-reads the target and
    finds it gone."""
    static_key, dynamic_key = await _seed_unreferenced_pair(db_session)
    service = build_groups_service()
    deleter_locked = asyncio.Event()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            signal_after_group_lock(session, deleter_locked)
            return await service.delete_group(session, static_key)

    async def add_first_reference() -> dict[str, Any] | None:
        await wait_for_group_lock(deleter_locked, label="updater")
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    delete_result, update_result = await asyncio.gather(delete_static(), add_first_reference(), return_exceptions=True)

    assert delete_result is True, f"deleter should have won: {delete_result!r}"
    assert isinstance(update_result, UnknownMemberOfError), (
        f"updater must reject a reference to the just-deleted group, got {update_result!r}"
    )
    await assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)


async def test_first_member_of_reference_wins_delete_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The updater serialises first; the deleter must then observe the
    reference rather than deleting out from under it.

    Historically the unguarded ordering: the deleter's predicate excluded the
    referring row before any locking, and nothing brought it back into
    consideration. The advisory lock closes it — see the module docstring.
    """
    static_key, dynamic_key = await _seed_unreferenced_pair(db_session)
    service = build_groups_service()
    updater_locked = asyncio.Event()

    async def add_first_reference() -> dict[str, Any] | None:
        async with db_session_maker() as session:
            signal_after_group_lock(session, updater_locked)
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        await wait_for_group_lock(updater_locked, label="deleter")
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    update_result, delete_result = await asyncio.gather(add_first_reference(), delete_static(), return_exceptions=True)

    assert not isinstance(update_result, Exception), f"updater should have won: {update_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the committed reference, got {delete_result!r}"
    )
    # assert_no_dangling_reference is vacuous on this ordering — the delete is
    # rejected, so its ``static_row is None`` guard never fires. Pin the exact
    # end state instead: both rows survive AND the updater's reference landed.
    # Without the last assertion an update_group regression that silently drops
    # the filters payload still leaves this test green.
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "the referenced static group must survive a rejected delete"
    assert dynamic_row is not None, "the referring dynamic group must survive"
    assert (dynamic_row.filters or {}).get("member_of") == [static_key], (
        f"the updater's member_of reference must have landed, got {dynamic_row.filters!r}"
    )
