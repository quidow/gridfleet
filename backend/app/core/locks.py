"""Named PostgreSQL advisory locks and the keyspace they live in."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

from app.core.observability import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

# The two-int namespace cannot collide with the single-bigint locks used elsewhere.
LOCK_NAMESPACE = 6000

#: Serialises every writer of ``device_groups`` *definitions*.
GROUP_MUTATION_LOCK_ID = 1


@asynccontextmanager
async def group_mutation_lock(db: AsyncSession, *, when: bool = True) -> AsyncIterator[None]:
    """Hold the transaction-scoped group lock and roll back if the body does not commit.

    ``when=False`` skips acquisition but keeps the same transaction-cleanup contract.
    """
    try:
        if when:
            await acquire_group_mutation_lock(db)
        yield
    finally:
        # Release the lock/no-op transaction without replacing a body exception.
        if db.in_transaction():
            try:
                await db.rollback()
            except Exception:
                logger.exception("group_mutation_lock_rollback_failed")


async def acquire_group_mutation_lock(db: AsyncSession) -> None:
    """Exclude group-definition writers until commit or rollback.

    Reads used for mutation must follow this call so READ COMMITTED sees the
    previous holder's commit. Import validation is the exception: bundle
    references are internal and key collisions are enforced by the unique index.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :lock_id)"),
        {"namespace": LOCK_NAMESPACE, "lock_id": GROUP_MUTATION_LOCK_ID},
    )
