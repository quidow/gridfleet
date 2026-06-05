"""drop appium node grid url

Revision ID: 9f2a7c84d1e6
Revises: 7c4d20b5a913
Create Date: 2026-06-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f2a7c84d1e6"
down_revision: str | None = "7c4d20b5a913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("appium_nodes", "grid_url")


def downgrade() -> None:
    # Re-added nullable (original was NOT NULL): the source data is gone, so a
    # faithful restore is impossible without a backfill.
    op.add_column(
        "appium_nodes",
        sa.Column("grid_url", sa.String(), nullable=True),
    )
