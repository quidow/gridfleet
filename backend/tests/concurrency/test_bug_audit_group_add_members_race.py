"""Bug 6: a concurrent duplicate ``(group, device)`` insert must stay benign.

``add_members`` used to issue an unlocked ``SELECT`` for membership existence
and then a plain ``db.add`` + commit. Two operator calls adding the same device
to the same group both passed the exists check — each snapshot predated the
other's insert — and both attempted the ``INSERT``, so the loser's commit
raised ``IntegrityError`` on the ``(group_id, device_id)`` unique constraint:
a 500 for a condition that should read as "already a member". The fix replaced
the pair with ``INSERT ... ON CONFLICT DO NOTHING``, and this test pins it.

Where the peer's duplicate can land, and why it cannot land later
-----------------------------------------------------------------
The obvious staging point — commit the duplicate *after* ``add_members`` has
taken its ``FOR UPDATE`` on the ``device_groups`` row — is unreachable, and the
reason is worth recording because it is not obvious from the Python.
``device_group_memberships.group_id`` carries a foreign key to
``device_groups.id``, so PostgreSQL makes every membership INSERT take
``FOR KEY SHARE`` on the parent row, and ``FOR KEY SHARE`` conflicts with
``FOR UPDATE``. While this call holds the group row, *no* session can commit a
membership row for that group — not a peer ``add_members``, not the portability
importer's staging pass. A peer that tried would block until this transaction
ended, which in a single-event-loop test that awaits the peer is a deadlock,
not a race.

So the duplicate is committed in the instant before the group read runs: after
the operator's request began, before the INSERT plans. Under READ COMMITTED
that is the whole of the window in which a conflicting row can appear, and it
is enough — the INSERT meets a committed row this call never saw, which is
exactly the collision the old code turned into a 500.

What this test does NOT establish
---------------------------------
It does not justify the ``FOR UPDATE``. ``ON CONFLICT DO NOTHING`` alone makes
the duplicate benign, so this test passes with ``for_update=True`` removed from
``add_members`` (verified, 2026-07-28). The lock is load-bearing for a
different interleaving — a concurrent ``delete_group``, where dropping it turns
a 404 into a foreign-key 500 — and that property is pinned by
``tests/concurrency/test_concurrency_group_membership_writers_vs_delete.py``.
Cite that file, not this one, for the lock.

The hook matches the group read *without* requiring ``FOR UPDATE`` in the SQL,
so the staging still fires if the lock is ever removed and this test keeps
reporting on the INSERT rather than on its own seam having gone quiet.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.devices.models import DeviceOperationalState
from app.devices.models.group import DeviceGroup, DeviceGroupMembership, GroupType
from tests.concurrency.group_lock_helpers import build_groups_service
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_add_members_races_concurrent_duplicate_insert(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="group-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    group = DeviceGroup(key=f"race-{uuid.uuid4().hex[:8]}", name="race", group_type=GroupType.static)
    db_session.add(group)
    await db_session.commit()

    group_id = group.id
    group_key = group.key
    device_id = device.id

    original_execute = db_session.execute
    original_scalar = db_session.scalar
    triggered = False

    async def _commit_peer_duplicate() -> None:
        async with db_session_maker() as side:
            side.add(DeviceGroupMembership(group_id=group_id, device_id=device_id))
            await side.commit()

    def _racing(delegate: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Wrap one session method so the first ``device_groups`` read is preceded
        by a peer committing the very membership row this call is about to insert."""

        async def _wrapper(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            nonlocal triggered
            stmt_text = " ".join(str(stmt).split()).lower()
            if not triggered and stmt_text.startswith("select") and "from device_groups" in stmt_text:
                triggered = True
                await _commit_peer_duplicate()
            return await delegate(stmt, *args, **kwargs)

        return _wrapper

    # Both methods are wrapped because ``_get_group_row`` reads through
    # ``db.scalar`` while the INSERT goes through ``db.execute``; an ``execute``-
    # only hook could never see the group read. ``triggered`` is asserted below
    # so a future move between the two fails loudly instead of going quiet.
    db_session.execute = _racing(original_execute)  # type: ignore[assignment, method-assign]
    db_session.scalar = _racing(original_scalar)  # type: ignore[assignment, method-assign]
    try:
        added = await build_groups_service().add_members(db_session, group_key, [device_id])
        await db_session.commit()
    except IntegrityError as exc:
        pytest.fail(f"add_members raised IntegrityError on concurrent duplicate insert: {exc}")
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]
        db_session.scalar = original_scalar  # type: ignore[method-assign]

    assert triggered, (
        "the peer never committed its duplicate: no SELECT against device_groups was intercepted, "
        "so add_members ran against no conflict and this test proved nothing"
    )
    assert added == 0, f"the INSERT met the peer's committed row and must have added nothing, got {added}"

    async with db_session_maker() as verify:
        rows = await verify.scalar(
            select(func.count(DeviceGroupMembership.id)).where(
                DeviceGroupMembership.group_id == group_id, DeviceGroupMembership.device_id == device_id
            )
        )
    assert rows == 1, f"exactly one membership row must survive the duplicate, got {rows}"
