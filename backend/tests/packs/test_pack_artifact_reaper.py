"""The janitor's ledger reaper: what it sweeps, what it spares, and when.

The stage is a backstop, exactly like pack drain -- the delete path unlinks
inline and drops its own row. What lands here is what crashed or failed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, update

from app.core.timeutil import now_utc
from app.packs.models import PackArtifact, PackArtifactState
from app.packs.services.artifact_reaper import (
    PACK_ARTIFACT_PENDING_GRACE_SEC,
    _drop_reaped,
    _select_candidates,
    run_pack_artifact_reaper_stage,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


async def _seed(
    sf: async_sessionmaker[AsyncSession],
    *,
    path: Path,
    state: PackArtifactState,
    age_sec: float = 0.0,
    write_file: bool = True,
) -> None:
    if write_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tarball-bytes")
    async with sf.begin() as db:
        db.add(PackArtifact(path=str(path), state=state))
        await db.flush()
        await db.execute(
            update(PackArtifact)
            .where(PackArtifact.path == str(path))
            .values(state_changed_at=now_utc() - timedelta(seconds=age_sec))
        )


async def _reap(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as db:
        await run_pack_artifact_reaper_stage(db)


async def test_orphaned_row_and_its_file_are_both_removed(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(db_session_maker, path=artifact, state=PackArtifactState.orphaned)

    await _reap(db_session_maker)

    assert not artifact.exists()
    assert (await db_session.scalars(select(PackArtifact))).all() == []


async def test_an_orphaned_row_whose_file_is_already_gone_still_clears(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A missing file counts as success -- the row is the thing left to clean."""
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(db_session_maker, path=artifact, state=PackArtifactState.orphaned, write_file=False)

    await _reap(db_session_maker)

    assert (await db_session.scalars(select(PackArtifact))).all() == []


async def test_pending_row_inside_its_grace_window_is_untouched(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The window is what stops the reaper racing a live upload mid-write."""
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(
        db_session_maker,
        path=artifact,
        state=PackArtifactState.pending,
        age_sec=PACK_ARTIFACT_PENDING_GRACE_SEC / 2,
    )

    await _reap(db_session_maker)

    assert artifact.exists()
    assert [row.state for row in (await db_session.scalars(select(PackArtifact))).all()] == [PackArtifactState.pending]


async def test_pending_row_past_its_grace_window_is_swept(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(
        db_session_maker,
        path=artifact,
        state=PackArtifactState.pending,
        age_sec=PACK_ARTIFACT_PENDING_GRACE_SEC + 1,
    )

    await _reap(db_session_maker)

    assert not artifact.exists()
    assert (await db_session.scalars(select(PackArtifact))).all() == []


async def test_active_rows_are_never_touched(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(
        db_session_maker,
        path=artifact,
        state=PackArtifactState.active,
        age_sec=PACK_ARTIFACT_PENDING_GRACE_SEC * 10,
    )

    await _reap(db_session_maker)

    assert artifact.exists()
    assert [row.state for row in (await db_session.scalars(select(PackArtifact))).all()] == [PackArtifactState.active]


async def test_the_unlink_runs_with_no_transaction_open(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the ledger: the filesystem effect is outside the boundary."""
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(db_session_maker, path=artifact, state=PackArtifactState.orphaned)

    observed: list[bool] = []
    real_unlink = Path.unlink

    def _spy(self: Path, *args: object, **kwargs: object) -> None:
        observed.append(reaper_session.in_transaction())
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _spy)
    async with db_session_maker() as reaper_session:
        await run_pack_artifact_reaper_stage(reaper_session)

    assert observed == [False], f"the reaper unlinked with a transaction open: {observed}"


async def test_a_row_re_reserved_between_the_read_and_the_delete_survives(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A concurrent re-upload owns the path; the reaper must not delete its row.

    Driven through the stage's own two helpers rather than through
    ``run_pack_artifact_reaper_stage``, because the interleaving has to land
    between them and the unlink spy in between is synchronous -- it cannot issue
    the competing write. The reaper holds no lock across that gap, so the row it
    read is stale from that instant on; the delete is paired on
    ``state_changed_at``, which every ledger write bumps, so a re-reserved row
    simply does not match.
    """
    artifact = tmp_path / "vendor-foo" / "0.1.0.tar.gz"
    await _seed(db_session_maker, path=artifact, state=PackArtifactState.orphaned)

    async with db_session_maker.begin() as db:
        claimed = await _select_candidates(db)
    async with db_session_maker.begin() as db:
        await db.execute(
            update(PackArtifact)
            .where(PackArtifact.path == str(artifact))
            .values(state=PackArtifactState.pending, state_changed_at=now_utc())
        )
    async with db_session_maker.begin() as db:
        dropped = await _drop_reaped(db, claimed)

    assert dropped == 0, "the reaper deleted a ledger row a re-upload had taken over"
    assert [row.state for row in (await db_session.scalars(select(PackArtifact))).all()] == [PackArtifactState.pending]


async def test_a_full_failed_batch_does_not_starve_a_newer_candidate(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed oldest rows rotate behind later work instead of poisoning every tick."""
    failed_at = now_utc() - timedelta(hours=2)
    failed_paths = [tmp_path / f"failed-{index}.tar.gz" for index in range(200)]
    successful_path = tmp_path / "successful.tar.gz"
    successful_path.write_bytes(b"tarball-bytes")
    async with db_session_maker.begin() as db:
        db.add_all(
            [
                PackArtifact(
                    path=str(path),
                    state=PackArtifactState.orphaned,
                    state_changed_at=failed_at,
                )
                for path in failed_paths
            ]
        )
        db.add(
            PackArtifact(
                path=str(successful_path),
                state=PackArtifactState.orphaned,
                state_changed_at=failed_at + timedelta(hours=1),
            )
        )

    real_unlink = Path.unlink

    def _fail_oldest(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("failed-"):
            raise PermissionError(f"cannot remove {self}")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _fail_oldest)

    await _reap(db_session_maker)
    await _reap(db_session_maker)

    assert not successful_path.exists()
    async with db_session_maker() as db:
        rows = (await db.scalars(select(PackArtifact))).all()
    assert len(rows) == 200
    assert {row.path for row in rows} == {str(path) for path in failed_paths}
