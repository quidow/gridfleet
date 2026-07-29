"""Create the ``pack_artifacts`` ledger and backfill it from existing releases.

One row per artifact file the system intends to keep. Existing artifacts are
backfilled as ``active``; a release whose file is already missing is left alone
and logged, because minting a ledger row for a file that is gone would make the
reaper the first thing to notice a pre-existing inconsistency and would give it
nothing to unlink.

This is the first revision in the chain that logs. It uses the standard
``alembic.runtime.migration`` logger so the line lands in the same stream as
alembic's own progress output.

Revision ID: 20260729_pack_artifacts
Revises: 6d8c3b5042b5
Create Date: 2026-07-29
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "20260729_pack_artifacts"
down_revision: str | None = "6d8c3b5042b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _backfill(bind: sa.Connection) -> None:
    # DISTINCT ON: artifact_path is a plain column, so nothing at the schema
    # level stops two releases naming the same file. The ledger's path is
    # unique, so collapse duplicates to the newest release rather than failing
    # the migration on data that is legal today.
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT ON (artifact_path) artifact_path, artifact_sha256 "
            "FROM driver_pack_releases WHERE artifact_path IS NOT NULL "
            "ORDER BY artifact_path, created_at DESC"
        )
    ).all()
    for artifact_path, artifact_sha256 in rows:
        path = Path(artifact_path)
        if not path.is_file():
            logger.warning("pack_artifacts backfill: no file at %s; leaving it out of the ledger", artifact_path)
            continue
        bind.execute(
            sa.text(
                "INSERT INTO pack_artifacts (id, path, sha256, size_bytes, state) "
                "VALUES (:id, :path, :sha256, :size_bytes, 'active')"
            ),
            {
                "id": str(uuid.uuid4()),
                "path": artifact_path,
                "sha256": artifact_sha256,
                "size_bytes": path.stat().st_size,
            },
        )


def upgrade() -> None:
    op.create_table(
        "pack_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "state",
            sa.Enum("pending", "active", "orphaned", name="packartifactstate", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pack_artifacts_pkey"),
        sa.UniqueConstraint("path", name="pack_artifacts_path_key"),
    )
    _backfill(op.get_bind())


def downgrade() -> None:
    # native_enum=False means the state column is a VARCHAR + CHECK, so there is
    # no Postgres type left behind to drop.
    op.drop_table("pack_artifacts")
