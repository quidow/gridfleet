"""add device intent registry

Revision ID: f2a9c7d4e6b1
Revises: bb1160a9697a
Create Date: 2026-05-12 19:45:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f2a9c7d4e6b1"
down_revision = "bb1160a9697a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("axis", sa.String(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["test_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "source", name="uq_device_intent_source"),
    )
    op.create_index("ix_device_intents_axis", "device_intents", ["axis"])
    op.create_index("ix_device_intents_device_axis", "device_intents", ["device_id", "axis"])
    op.create_index("ix_device_intents_device_id", "device_intents", ["device_id"])
    op.create_index("ix_device_intents_expires_at", "device_intents", ["expires_at"])
    op.create_index("ix_device_intents_run_id", "device_intents", ["run_id"])
    op.create_index("ix_device_intents_source", "device_intents", ["source"])

    op.create_table(
        "device_intent_dirty",
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dirty_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id"),
    )

    op.create_table(
        "agent_reconfigure_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("accepting_new_sessions", sa.Boolean(), nullable=False),
        sa.Column("stop_pending", sa.Boolean(), nullable=False),
        sa.Column("grid_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reconciled_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_reconfigure_outbox_undelivered",
        "agent_reconfigure_outbox",
        ["device_id", "delivered_at"],
    )
    op.create_index("ix_agent_reconfigure_outbox_device_id", "agent_reconfigure_outbox", ["device_id"])

    op.add_column(
        "appium_nodes",
        sa.Column("accepting_new_sessions", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("stop_pending", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "devices",
        sa.Column("recovery_allowed", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column("devices", sa.Column("recovery_blocked_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "recovery_blocked_reason")
    op.drop_column("devices", "recovery_allowed")
    op.drop_column("appium_nodes", "generation")
    op.drop_column("appium_nodes", "stop_pending")
    op.drop_column("appium_nodes", "accepting_new_sessions")
    op.drop_index("ix_agent_reconfigure_outbox_device_id", table_name="agent_reconfigure_outbox")
    op.drop_index("ix_agent_reconfigure_outbox_undelivered", table_name="agent_reconfigure_outbox")
    op.drop_table("agent_reconfigure_outbox")
    op.drop_table("device_intent_dirty")
    op.drop_index("ix_device_intents_source", table_name="device_intents")
    op.drop_index("ix_device_intents_run_id", table_name="device_intents")
    op.drop_index("ix_device_intents_expires_at", table_name="device_intents")
    op.drop_index("ix_device_intents_device_id", table_name="device_intents")
    op.drop_index("ix_device_intents_device_axis", table_name="device_intents")
    op.drop_index("ix_device_intents_axis", table_name="device_intents")
    op.drop_table("device_intents")
