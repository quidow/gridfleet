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

    Must be taken *before* any ``device_groups`` read. Under READ COMMITTED each
    statement takes a fresh snapshot, so a read issued after this call sees
    everything the previous holder committed — which is the whole guarantee. A
    read taken before the lock carries a stale snapshot and defeats it.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :lock_id)"),
        {"namespace": LOCK_NAMESPACE, "lock_id": GROUP_MUTATION_LOCK_ID},
    )
