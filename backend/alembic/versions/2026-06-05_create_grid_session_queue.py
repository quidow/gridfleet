"""create grid_session_queue table

Revision ID: 7c4d20b5a913
Revises: 3fe8a129e081
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c4d20b5a913"
down_revision: str | None = "3fe8a129e081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "grid_session_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("requested_body", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("waiting", "claimed", "cancelled", "expired", name="gridqueuestatus"),
            nullable=False,
        ),
        sa.Column("session_row_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_grid_session_queue_status", "grid_session_queue", ["status"])


def downgrade() -> None:
    op.drop_table("grid_session_queue")
    op.execute("DROP TYPE IF EXISTS gridqueuestatus")
