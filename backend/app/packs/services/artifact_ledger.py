"""The four ledger writes. Transaction-local: the caller owns the boundary.

Each write is one statement so it composes into whatever transaction the upload
or delete path already has open. Nothing here touches the filesystem -- that is
the point of the ledger: the record of intent lands in one transaction and the
bytes move with no transaction open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert

from app.core.timeutil import now_utc
from app.packs.models import PackArtifact, PackArtifactState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def reserve_artifact(db: AsyncSession, *, path: str) -> None:
    """Claim *path* for an upload whose bytes are not written yet.

    Upsert, not insert: a pack+release maps to the same path forever, so a
    re-upload -- or a retry after a crash -- meets a row that is already there.
    The ``where`` is what keeps an ``active`` row out of the reaper's reach: if
    a live release already names this file, demoting it to ``pending`` and then
    dying would leave the reaper free to delete a file that is still wanted.
    """
    now = now_utc()
    await db.execute(
        insert(PackArtifact)
        .values(path=path, state=PackArtifactState.pending, state_changed_at=now)
        .on_conflict_do_update(
            index_elements=[PackArtifact.path],
            set_={"state": PackArtifactState.pending, "state_changed_at": now},
            where=PackArtifact.state != PackArtifactState.active,
        )
    )


async def activate_artifact(db: AsyncSession, *, path: str, sha256: str, size_bytes: int) -> None:
    """Promote a reservation to a finished file, with its identity filled in.

    Runs in the same transaction as the release metadata that names the file, so
    a release row can never point at a file whose ledger entry says it was never
    finished.
    """
    await db.execute(
        update(PackArtifact)
        .where(PackArtifact.path == path)
        .values(
            state=PackArtifactState.active,
            sha256=sha256,
            size_bytes=size_bytes,
            state_changed_at=now_utc(),
        )
    )


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
