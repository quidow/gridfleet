"""Deleting a static group must never race a concurrent *first* ``member_of``
reference to it into a dangling reference.

``delete_group`` locks the target plus every group whose ``filters['member_of']``
is already non-NULL. A review claimed that a concurrent ``update_group`` adding
the *first* reference to the target escapes that predicate — the referring row
does not yet carry a ``member_of``, so the deleter never locks it — leaving a
dynamic group pointing at a deleted key.

The target's row lock closes only *one* of the two orderings:

* deleter first — the updater blocks on the target's row lock, and its select
  returns *without* the deleted target once the deleter commits, so
  ``_assert_member_of_resolves`` raises ``UnknownMemberOfError``. Guarded.
* updater first — NOT guarded. The updater locks the dynamic group and the
  target, but does not write the dynamic row until commit. The deleter's
  statement then plans against a snapshot in which the dynamic row still has a
  NULL ``member_of``, so the ``member_of IS NOT NULL`` predicate filters it out
  *before* any locking. The deleter blocks on the target only, and under READ
  COMMITTED, Postgres re-evaluates (EvalPlanQual) just that one row when the
  updater commits — the excluded dynamic row is never reconsidered. Both
  transactions commit and the dynamic group is left referencing a deleted key.

``test_first_member_of_reference_wins_delete_is_rejected`` currently FAILS,
reproducing that dangling reference. It is written against the intended
invariant, not the current behaviour, and should go green with the fix.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services.groups import (
    DeviceGroupsService,
    GroupReferencedError,
    UnknownMemberOfError,
)
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Long enough for the peer's blocked ``FOR UPDATE`` to reach the lock manager
# before the holder commits. Only widens the race window; correctness does not
# depend on the exact value.
_HANDOFF_SEC = 0.5


def _service() -> DeviceGroupsService:
    settings = FakeSettingsReader({})
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(settings=settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
        settings=settings,
    )


async def _seed_unreferenced_pair(db_session: AsyncSession) -> tuple[str, str]:
    """A static group with *no* referrers and a dynamic group with no ``member_of``.

    This is precisely the state the review's interleaving starts from: the
    dynamic group is invisible to ``delete_group``'s ``member_of IS NOT NULL``
    predicate until the concurrent update commits.
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


def _signal_after_group_lock(session: AsyncSession, locked: asyncio.Event) -> None:
    """Set *locked* once *session* has issued its ``device_groups`` ``FOR UPDATE``.

    Then hold inside the interception for ``_HANDOFF_SEC`` so the peer
    transaction can start its own statement and block on the row lock this
    session now holds.
    """
    original_execute = session.execute
    fired = False

    async def _intercepted(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(stmt, *args, **kwargs)
        text = str(stmt).lower()
        if not fired and "device_groups" in text and "for update" in text:
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
        assert dynamic_row is not None
        if static_row is None:
            member_of = (dynamic_row.filters or {}).get("member_of", [])
            assert static_key not in member_of, (
                f"dynamic group {dynamic_key} references deleted static group {static_key}"
            )


async def test_delete_wins_first_member_of_reference_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Deleter takes the target's row lock first; the updater blocks, then
    re-reads the target and finds it gone."""
    static_key, dynamic_key = await _seed_unreferenced_pair(db_session)
    service = _service()
    deleter_locked = asyncio.Event()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            _signal_after_group_lock(session, deleter_locked)
            return await service.delete_group(session, static_key)

    async def add_first_reference() -> DeviceGroup | None:
        await deleter_locked.wait()
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
    await _assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)


async def test_first_member_of_reference_wins_delete_is_rejected(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Updater takes the target's row lock first; the deleter must then observe
    the reference rather than deleting out from under it.

    This is the ordering the review argued was unguarded, and it is: at the
    moment the deleter's statement plans, the referring row still has a NULL
    ``member_of`` and so falls outside its predicate, and nothing brings it back
    into consideration. Currently FAILS — see the module docstring.
    """
    static_key, dynamic_key = await _seed_unreferenced_pair(db_session)
    service = _service()
    updater_locked = asyncio.Event()

    async def add_first_reference() -> DeviceGroup | None:
        async with db_session_maker() as session:
            _signal_after_group_lock(session, updater_locked)
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [static_key]}),  # type: ignore[arg-type]
            )

    async def delete_static() -> bool:
        await updater_locked.wait()
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    update_result, delete_result = await asyncio.gather(add_first_reference(), delete_static(), return_exceptions=True)

    assert not isinstance(update_result, Exception), f"updater should have won: {update_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the committed reference, got {delete_result!r}"
    )
    await _assert_no_dangling_reference(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
