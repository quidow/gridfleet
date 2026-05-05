"""add device health columns and drop legacy snapshot KV

Revision ID: ff830fddabf1
Revises: e1a3b7c5d9f2
Create Date: 2026-05-05 09:52:42.286599

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "ff830fddabf1"
down_revision = "e1a3b7c5d9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("device_checks_healthy", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("device_checks_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("device_checks_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("session_viability_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("session_viability_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("session_viability_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("emulator_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "appium_nodes",
        sa.Column(
            "consecutive_health_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("last_health_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("health_running", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("health_state", sa.Text(), nullable=True),
    )

    # Alpha cleanup: drop the legacy snapshot + counter KV rows. No backfill.
    # Deploy the migration and the new code together - no rolling deploy
    # window where the old code is still reading these namespaces.
    op.execute(
        "DELETE FROM control_plane_state_entries "
        "WHERE namespace IN ('device.health_summary', 'node_health.failure_count')"
    )


def downgrade() -> None:
    op.drop_column("devices", "emulator_state")
    op.drop_column("devices", "session_viability_checked_at")
    op.drop_column("devices", "session_viability_error")
    op.drop_column("devices", "session_viability_status")
    op.drop_column("devices", "device_checks_checked_at")
    op.drop_column("devices", "device_checks_summary")
    op.drop_column("devices", "device_checks_healthy")
    op.drop_column("appium_nodes", "health_state")
    op.drop_column("appium_nodes", "health_running")
    op.drop_column("appium_nodes", "last_health_checked_at")
    op.drop_column("appium_nodes", "consecutive_health_failures")
