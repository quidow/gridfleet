"""drop appium plugins feature

Removes the Appium-plugins feature: the ``appium_plugins`` registry table, the
``host_plugin_runtime_statuses`` per-host status table, and the now-unused
``host_runtime_installations.plugin_specs`` column.

Revision ID: f1a2b3c4d5e6
Revises: c7d2f4a9b1e0
Create Date: 2026-06-25

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a2b3c4d5e6"
down_revision = "c7d2f4a9b1e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("host_runtime_installations", "plugin_specs")
    op.drop_table("host_plugin_runtime_statuses")
    op.drop_table("appium_plugins")  # drops the appium_plugins_name_idx index with it


def downgrade() -> None:
    op.create_table(
        "appium_plugins",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("package", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notes", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("appium_plugins_name_idx", "appium_plugins", ["name"], unique=True)
    op.create_table(
        "host_plugin_runtime_statuses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("host_id", sa.UUID(), nullable=False),
        sa.Column("runtime_id", sa.String(), nullable=False),
        sa.Column("plugin_name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("blocked_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "runtime_id", "plugin_name", name="host_plugin_runtime_statuses_uq"),
    )
    op.add_column(
        "host_runtime_installations",
        sa.Column(
            "plugin_specs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
