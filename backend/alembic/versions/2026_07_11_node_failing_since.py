"""Replace node-health failure counts with an observation timestamp."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260711_node_failing_since"
down_revision = "20260711_fail_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appium_nodes", sa.Column("health_failing_since", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("appium_nodes", "consecutive_health_failures")


def downgrade() -> None:
    op.add_column(
        "appium_nodes",
        sa.Column("consecutive_health_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("appium_nodes", "health_failing_since")
