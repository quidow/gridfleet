"""add sessions.ticket_id resume key

Revision ID: 8d029f5a4b72
Revises: 49e8065414a1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8d029f5a4b72"
down_revision: str | None = "49e8065414a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("ticket_id", sa.UUID(), nullable=True))
    op.create_index(
        "ux_sessions_ticket_id_live",
        "sessions",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL AND ticket_id IS NOT NULL"),
    )
    # Backfill live claimed tickets so resume keeps working across the deploy.
    op.execute(
        "UPDATE sessions SET ticket_id = t.id "
        "FROM grid_session_queue t "
        "WHERE t.session_row_id = sessions.id AND t.status = 'claimed'"
    )


def downgrade() -> None:
    op.drop_index("ux_sessions_ticket_id_live", table_name="sessions")
    op.drop_column("sessions", "ticket_id")
