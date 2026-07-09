"""drop grid queue ticket session row id

Revision ID: 2f0e0d84a638
Revises: 8d029f5a4b72
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2f0e0d84a638"
down_revision: str | None = "8d029f5a4b72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No row may carry the retired 'claimed' label once the Python enum drops it
    # (SQLAlchemy validates reads against the Python enum). Terminalize instead
    # of deleting so retention (data_cleanup) purges them on its own schedule.
    op.execute("UPDATE grid_session_queue SET status = 'expired' WHERE status = 'claimed'")
    op.drop_index("grid_session_queue_session_row_id_idx", table_name="grid_session_queue")
    op.drop_column("grid_session_queue", "session_row_id")
    # The native PG type 'gridqueuestatus' keeps its dead 'claimed' label --
    # Postgres cannot drop an enum label in place. Harmless: no code path can
    # write or read it.


def downgrade() -> None:
    op.add_column(
        "grid_session_queue",
        sa.Column("session_row_id", sa.UUID(), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("grid_session_queue_session_row_id_idx", "grid_session_queue", ["session_row_id"])
    # claimed-row data is not recoverable; downgrade restores schema only.
