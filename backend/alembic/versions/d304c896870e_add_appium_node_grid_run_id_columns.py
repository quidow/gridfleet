"""add appium node grid run id columns

Revision ID: d304c896870e
Revises: d8e1f4a9b2c7
Create Date: 2026-05-11 16:54:14.837509

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "d304c896870e"
down_revision = "d8e1f4a9b2c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appium_nodes",
        sa.Column("desired_grid_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("grid_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_appium_nodes_desired_grid_run_id",
        "appium_nodes",
        ["desired_grid_run_id"],
    )
    op.execute(
        """
        UPDATE appium_nodes AS an
        SET desired_grid_run_id = dr.run_id
        FROM device_reservations AS dr
        JOIN test_runs AS tr ON tr.id = dr.run_id
        WHERE dr.device_id = an.device_id
          AND dr.released_at IS NULL
          AND tr.state IN ('preparing', 'ready', 'active')
        """
    )


def downgrade() -> None:
    op.drop_index("ix_appium_nodes_desired_grid_run_id", table_name="appium_nodes")
    op.drop_column("appium_nodes", "grid_run_id")
    op.drop_column("appium_nodes", "desired_grid_run_id")
