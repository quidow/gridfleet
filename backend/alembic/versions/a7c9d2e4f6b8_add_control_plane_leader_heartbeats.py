"""add control_plane_leader_heartbeats

Revision ID: a7c9d2e4f6b8
Revises: 1194c5272004
Create Date: 2026-05-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "a7c9d2e4f6b8"
down_revision = "1194c5272004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_plane_leader_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("holder_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lock_backend_pid", sa.Integer(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Seed the singleton row so UPDATE ... WHERE id = 1 has something to hit.
    # We INSERT ... ON CONFLICT DO NOTHING so this is idempotent if a future
    # downgrade-then-upgrade leaves the row.
    op.execute(
        "INSERT INTO control_plane_leader_heartbeats (id, holder_id) "
        "VALUES (1, gen_random_uuid()) ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("control_plane_leader_heartbeats")
