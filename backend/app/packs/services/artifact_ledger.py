"""The four ledger writes. Transaction-local: the caller owns the boundary.

Each write is one statement so it composes into whatever transaction the upload
or delete path already has open. Nothing here touches the filesystem -- that is
the point of the ledger: the record of intent lands in one transaction and the
bytes move with no transaction open.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert

from app.core.timeutil import now_utc
from app.packs.models import PackArtifact, PackArtifactState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


async def reserve_artifact(db: AsyncSession, *, path: str) -> tuple[uuid.UUID, datetime] | None:
    """Claim *path* for an upload whose bytes are not written yet.

    A new row or an orphan is claimed; an existing pending or active row returns
    no claim. The returned id/timestamp pair fences activation to this attempt.
    """
    claim_id = uuid.uuid4()
    now = now_utc()
    result = await db.execute(
        insert(PackArtifact)
        .values(id=claim_id, path=path, state=PackArtifactState.pending, state_changed_at=now)
        .on_conflict_do_update(
            index_elements=[PackArtifact.path],
            set_={
                "id": claim_id,
                "state": PackArtifactState.pending,
                "sha256": None,
                "size_bytes": None,
                "state_changed_at": now,
            },
            where=PackArtifact.state == PackArtifactState.orphaned,
        )
        .returning(PackArtifact.id, PackArtifact.state_changed_at)
    )
    row = result.one_or_none()
    return None if row is None else (row.id, row.state_changed_at)


async def activate_artifact(
    db: AsyncSession,
    *,
    artifact_id: uuid.UUID,
    path: str,
    sha256: str,
    size_bytes: int,
    reserved_at: datetime | None,
) -> bool:
    """Promote the caller's reservation, or refresh the matching active artifact.

    Runs in the same transaction as the release metadata that names the file, so
    a release row can never point at a file whose ledger entry says it was never
    finished.
    """
    statement = update(PackArtifact).where(PackArtifact.id == artifact_id, PackArtifact.path == path)
    if reserved_at is None:
        statement = statement.where(
            PackArtifact.state == PackArtifactState.active,
            PackArtifact.sha256 == sha256,
        )
    else:
        statement = statement.where(
            PackArtifact.state == PackArtifactState.pending,
            PackArtifact.state_changed_at == reserved_at,
        )
    result = await db.execute(
        statement.values(
            state=PackArtifactState.active,
            sha256=sha256,
            size_bytes=size_bytes,
            state_changed_at=now_utc(),
        ).returning(PackArtifact.id)
    )
    return result.scalar_one_or_none() is not None


async def orphan_artifacts(db: AsyncSession, *, paths: Sequence[str]) -> None:
    """Mark files as garbage in the same transaction that drops their metadata."""
    if not paths:
        return
    await db.execute(
        update(PackArtifact)
        .where(PackArtifact.path.in_(list(paths)))
        .values(state=PackArtifactState.orphaned, state_changed_at=now_utc())
    )


async def forget_artifacts(db: AsyncSession, *, paths: Sequence[str]) -> None:
    """Drop ledger rows whose files are gone. Called after a successful unlink.

    Guarded on ``orphaned`` because a re-upload of the same release can win the
    path back between the unlink and this call; an unconditional delete would
    drop the ledger row for a file that is about to go live.
    """
    if not paths:
        return
    await db.execute(
        delete(PackArtifact).where(
            PackArtifact.path.in_(list(paths)),
            PackArtifact.state == PackArtifactState.orphaned,
        )
    )
