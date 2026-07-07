"""drop leader heartbeats and settings

Revision ID: 9053c8d3caa2
Revises: a991cd9df7b5
Create Date: 2026-07-07 23:04:00.791592

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9053c8d3caa2'
down_revision: Union[str, None] = 'a991cd9df7b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("control_plane_leader_heartbeats")
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'general.leader_keepalive_enabled',"
        "'general.leader_keepalive_interval_sec',"
        "'general.leader_stale_threshold_sec')"
    )


def downgrade() -> None:
    # Restore the table schema so a downgrade to a pre-split revision leaves the
    # old code's ORM model with a table to map. The DELETEd settings were
    # override rows (runtime data) — the old registry re-supplies defaults, so
    # they are not re-seeded here.
    op.create_table(
        "control_plane_leader_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holder_id", sa.UUID(), nullable=False),
        sa.Column("lock_backend_pid", sa.Integer(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
