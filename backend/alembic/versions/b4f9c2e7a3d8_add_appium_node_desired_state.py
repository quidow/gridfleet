"""add appium_node desired_state columns

Revision ID: b4f9c2e7a3d8
Revises: 248d82475c7d
Create Date: 2026-05-10 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4f9c2e7a3d8"
down_revision: str | None = "248d82475c7d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    nodestate_enum = postgresql.ENUM(name="nodestate", create_type=False)

    op.add_column(
        "appium_nodes",
        sa.Column(
            "desired_state",
            nodestate_enum,
            nullable=False,
            server_default=sa.text("'stopped'"),
        ),
    )
    op.add_column("appium_nodes", sa.Column("desired_port", sa.Integer(), nullable=True))
    op.add_column("appium_nodes", sa.Column("transition_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("appium_nodes", sa.Column("transition_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("appium_nodes", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True))

    op.execute("UPDATE appium_nodes SET desired_state = 'running', desired_port = port WHERE state = 'running'")

    op.create_check_constraint(
        "ck_appium_nodes_desired_state",
        "appium_nodes",
        "desired_state IN ('running', 'stopped')",
    )
    op.create_check_constraint(
        "ck_appium_nodes_desired_port_requires_running",
        "appium_nodes",
        "desired_state = 'running' OR desired_port IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_appium_nodes_desired_port_requires_running", "appium_nodes", type_="check", if_exists=True)
    op.drop_constraint("ck_appium_nodes_desired_state", "appium_nodes", type_="check", if_exists=True)
    op.drop_column("appium_nodes", "last_observed_at")
    op.drop_column("appium_nodes", "transition_deadline")
    op.drop_column("appium_nodes", "transition_token")
    op.drop_column("appium_nodes", "desired_port")
    op.drop_column("appium_nodes", "desired_state")
