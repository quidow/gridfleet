"""Named PostgreSQL advisory locks and the keyspace they live in."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Postgres keeps two independent advisory-lock spaces: one keyed by a single
# bigint, one keyed by a pair of int4s. The single-bigint space already has two
# uncoordinated occupants — ``control_plane_leader``'s hand-picked 6001
# (app/core/leader/advisory.py) and ``resource_service``'s hash-derived per-host
# lock. Locks declared here use the two-int space, which is otherwise unused, so
# they cannot collide with either. Add new ids to this module rather than
# picking a literal at a call site.
LOCK_NAMESPACE = 6000

#: Serialises every writer of ``device_groups`` *definitions*.
GROUP_MUTATION_LOCK_ID = 1


async def acquire_group_mutation_lock(db: AsyncSession) -> None:
    """Exclude every other group-definition writer for the rest of this transaction.

    Transaction-scoped: Postgres releases the lock on commit or rollback, so
    there is no unlock call and no leak path.

    Take this before any ``device_groups`` read whose result you act on. Under
    READ COMMITTED each statement takes a fresh snapshot, so a read issued after
    this call sees everything the previous holder committed; a read taken before
    it carries a stale snapshot. That ordering is what makes the ``member_of``
    invariant hold in :mod:`app.devices.services.groups`.

    The portability importer deliberately reads ``device_groups`` *before*
    taking this lock: ``validate_bundle`` issues per-row queries, and holding a
    fleet-global lock for the length of a large bundle's validation would cost
    far more than the race it closes. That is sound there because a bundle's
    ``member_of`` may only name static groups defined in the same bundle and
    inserted in the same transaction, and because the loser of a key-collision
    race is caught by the ``ix_device_groups_key`` unique index and surfaced as
    a 409 rather than by this lock. A new caller that does not satisfy both
    conditions must take the lock first.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :lock_id)"),
        {"namespace": LOCK_NAMESPACE, "lock_id": GROUP_MUTATION_LOCK_ID},
    )
