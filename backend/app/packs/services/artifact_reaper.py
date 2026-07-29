"""Janitor stage: converge the artifact ledger with the filesystem.

Backstop only, exactly like pack drain. The delete routes unlink inline and drop
their own ledger rows; what reaches this stage is what crashed or failed -- an
unlink that raised, and a reservation whose upload died between reserve and
activate.

Three transactions' worth of work in two boundaries with the filesystem effect
in between, because that is the invariant the ledger exists to hold: no
filesystem write while a transaction is open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, or_, select

from app.core.metrics_recorders import record_pack_artifacts_reaped
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.packs.models import PackArtifact, PackArtifactState
from app.packs.services.service import unlink_pack_artifact

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# Plumbing constants, never registry settings (design P5). The grace window has
# to exceed the longest plausible upload: the tarball cap is 50 MiB and the
# bytes are already in memory when the reservation commits, so 15 minutes is
# orders of magnitude of headroom, and its only cost is how long a crashed
# upload's file lingers.
PACK_ARTIFACT_REAP_STAGE_INTERVAL_SEC = 300.0
PACK_ARTIFACT_PENDING_GRACE_SEC = 900.0
PACK_ARTIFACT_REAP_BATCH = 200


@dataclass(frozen=True, slots=True)
class _ReapCandidate:
    id: uuid.UUID
    path: str
    state_changed_at: datetime


async def _select_candidates(db: AsyncSession) -> list[_ReapCandidate]:
    cutoff = now_utc() - timedelta(seconds=PACK_ARTIFACT_PENDING_GRACE_SEC)
    rows = (
        await db.execute(
            select(PackArtifact.id, PackArtifact.path, PackArtifact.state_changed_at)
            .where(
                or_(
                    PackArtifact.state == PackArtifactState.orphaned,
                    and_(
                        PackArtifact.state == PackArtifactState.pending,
                        PackArtifact.state_changed_at < cutoff,
                    ),
                )
            )
            .order_by(PackArtifact.state_changed_at)
            .limit(PACK_ARTIFACT_REAP_BATCH)
        )
    ).all()
    return [_ReapCandidate(id=row.id, path=row.path, state_changed_at=row.state_changed_at) for row in rows]


async def _drop_reaped(db: AsyncSession, reaped: list[_ReapCandidate]) -> int:
    """Delete only the rows that are still exactly as they were read.

    Paired on ``(id, state_changed_at)``: every ledger write bumps
    ``state_changed_at``, so a row an upload re-reserved between the read and
    here no longer matches and survives. Deleting unconditionally would drop the
    ledger entry for a file that is about to go live.
    """
    if not reaped:
        return 0
    result = await db.execute(
        delete(PackArtifact).where(
            or_(
                *[
                    and_(PackArtifact.id == row.id, PackArtifact.state_changed_at == row.state_changed_at)
                    for row in reaped
                ]
            )
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def run_pack_artifact_reaper_stage(db: AsyncSession) -> None:
    async with db.begin():
        candidates = await _select_candidates(db)
    if not candidates:
        return

    # No transaction is open across this loop, which is the point.
    reaped = [row for row in candidates if unlink_pack_artifact(row.path)]
    if not reaped:
        return

    async with db.begin():
        dropped = await _drop_reaped(db, reaped)

    record_pack_artifacts_reaped(dropped)
    if dropped != len(reaped):
        logger.info("pack_artifact_reap_superseded", claimed=len(reaped), dropped=dropped)
