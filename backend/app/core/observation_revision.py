"""Backend-issued monotonic revision source for the two-axis health guard.

The two moved observation folds (node health, device health) race synchronous
higher-authority writers (restart-event ingest, host-offline cascade, lifecycle
crash actions, create-failure). To order them, every writer of the two axes
takes a *revision* under the row lock and applies its verdict only when the
revision is strictly greater than the axis's stored revision. A synchronous
event drawn at write time always out-ranks a stale fold observation whose
revision was drawn earlier at ingest, so the stale observation loses the
comparison and skips its regressing write.

A single PostgreSQL sequence backs the revision: strictly monotonic and
non-transactional (``nextval`` never rolls back, so a rolled-back push leaves a
gap but never regresses), shared across every API worker and the scheduler. It
is attached to ``Base.metadata`` so the per-test schema's ``create_all``
provisions it alongside the tables, and emitted explicitly by the Alembic
migration for real databases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Sequence, select

from app.core.database import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OBSERVATION_REVISION_SEQUENCE = "observation_revision_seq"

observation_revision_seq = Sequence(OBSERVATION_REVISION_SEQUENCE, metadata=Base.metadata)


async def next_observation_revision(db: AsyncSession) -> int:
    """Draw the next strictly-greater observation revision from the sequence."""
    return (await db.execute(select(observation_revision_seq.next_value()))).scalar_one()
