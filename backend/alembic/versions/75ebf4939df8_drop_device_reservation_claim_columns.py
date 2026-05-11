"""drop device reservation claim columns

Revision ID: 75ebf4939df8
Revises: d304c896870e
Create Date: 2026-05-11 16:56:17.999061

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "75ebf4939df8"
down_revision = "d304c896870e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE test_runs SET state = 'active' WHERE state = 'ready'")
    op.drop_column("device_reservations", "claimed_by")
    op.drop_column("device_reservations", "claimed_at")


def downgrade() -> None:
    op.add_column("device_reservations", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("device_reservations", sa.Column("claimed_by", sa.String(), nullable=True))
