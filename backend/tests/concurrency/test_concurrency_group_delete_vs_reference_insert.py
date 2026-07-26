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
  to a SAVEPOINT (keeping the transaction-scoped advisory lock and the loaded
  rows) rather than to the root.

Both interleavings are driven by committing real rows from a second session at a
known point, so the ``DELETE`` hits the genuine RESTRICT trigger. Nothing here
patches the failure itself — only the point at which the peer commits.

Today the group-mutation advisory lock keeps every in-app writer off this branch.
It is nonetheless the designed replacement for that lock, so it has to work
before the lock goes away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf, GroupType
from app.devices.services import groups as group_service
from app.devices.services.groups import GroupReferencedError
from tests.concurrency.group_lock_helpers import build_groups_service

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
