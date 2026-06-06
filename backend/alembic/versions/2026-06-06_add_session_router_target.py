"""add sessions.router_target and grid_session_queue (status, created_at) index

Revision ID: a1b2c3d4e5f6
Revises: 9f2a7c84d1e6
Create Date: 2026-06-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9f2a7c84d1e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("router_target", sa.String(), nullable=True))
    op.create_index(
        "ix_grid_session_queue_status_created_at",
        "grid_session_queue",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_grid_session_queue_status_created_at", table_name="grid_session_queue")
    op.drop_column("sessions", "router_target")
