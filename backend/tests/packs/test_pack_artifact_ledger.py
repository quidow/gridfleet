"""The four ledger writes, and the two guards that keep them safe under a re-upload."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.packs.models import PackArtifact, PackArtifactState
from app.packs.services.artifact_ledger import (
    activate_artifact,
    forget_artifacts,
    orphan_artifacts,
    reserve_artifact,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.db

_PATH = "/var/lib/gridfleet/driver-packs/vendor-foo/0.1.0.tar.gz"


async def _row(db: AsyncSession) -> PackArtifact:
    """Read the ledger row fresh.

    ``expire_all`` is not optional: every ledger write is a Core statement, so
    an ORM instance already in the identity map keeps its stale attributes
    through a plain ``select`` and the assertions below would read the values
    the previous phase left there.
    """
    db.expire_all()
    return (await db.execute(select(PackArtifact).where(PackArtifact.path == _PATH))).scalar_one()


async def test_reserve_then_activate_records_sha_and_size(db_session: AsyncSession) -> None:
    await reserve_artifact(db_session, path=_PATH)
    reserved = await _row(db_session)
    assert reserved.state is PackArtifactState.pending
    assert reserved.sha256 is None
    assert reserved.size_bytes is None

    await activate_artifact(db_session, path=_PATH, sha256="abc123", size_bytes=4096)
    activated = await _row(db_session)

    assert activated.state is PackArtifactState.active
    assert activated.sha256 == "abc123"
    assert activated.size_bytes == 4096
    assert activated.state_changed_at >= reserved.state_changed_at


async def test_reserve_is_idempotent_for_a_pending_row(db_session: AsyncSession) -> None:
    await reserve_artifact(db_session, path=_PATH)
    first = (await _row(db_session)).state_changed_at

    await reserve_artifact(db_session, path=_PATH)

    db_session.expire_all()
    rows = (await db_session.execute(select(PackArtifact))).scalars().all()
    assert len(rows) == 1, "a second reservation must reuse the row, not duplicate the path"
    assert rows[0].state is PackArtifactState.pending
    assert rows[0].state_changed_at >= first


async def test_reserve_never_demotes_an_active_row(db_session: AsyncSession) -> None:
    """A live release's file must not become reapable because an upload retried."""
    await reserve_artifact(db_session, path=_PATH)
    await activate_artifact(db_session, path=_PATH, sha256="abc123", size_bytes=4096)

    await reserve_artifact(db_session, path=_PATH)
    row = await _row(db_session)

    assert row.state is PackArtifactState.active
    assert row.sha256 == "abc123", "the reservation must not blank an active row's identity"


async def test_orphan_then_forget_removes_the_row(db_session: AsyncSession) -> None:
    await reserve_artifact(db_session, path=_PATH)
    await activate_artifact(db_session, path=_PATH, sha256="abc123", size_bytes=4096)

    await orphan_artifacts(db_session, paths=[_PATH])
    assert (await _row(db_session)).state is PackArtifactState.orphaned

    await forget_artifacts(db_session, paths=[_PATH])

    db_session.expire_all()
    assert (await db_session.execute(select(PackArtifact))).scalars().all() == []


async def test_forget_leaves_a_path_a_new_upload_has_re_reserved(db_session: AsyncSession) -> None:
    """The delete path's forget must not eat the row a concurrent re-upload owns."""
    await reserve_artifact(db_session, path=_PATH)
    await activate_artifact(db_session, path=_PATH, sha256="abc123", size_bytes=4096)
    await orphan_artifacts(db_session, paths=[_PATH])
    # The re-upload wins the path back between the unlink and the forget.
    await reserve_artifact(db_session, path=_PATH)

    await forget_artifacts(db_session, paths=[_PATH])

    assert (await _row(db_session)).state is PackArtifactState.pending


async def test_orphan_and_forget_are_no_ops_for_an_empty_path_list(db_session: AsyncSession) -> None:
    await orphan_artifacts(db_session, paths=[])
    await forget_artifacts(db_session, paths=[])

    assert (await db_session.execute(select(PackArtifact))).scalars().all() == []
