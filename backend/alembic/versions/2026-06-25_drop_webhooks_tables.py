"""drop webhooks feature tables

Revision ID: c7d2f4a9b1e0
Revises: 54be551fc505
Create Date: 2026-06-25

The webhooks feature was removed. Drop the webhook delivery + registration
tables. webhook_deliveries goes first (FKs into webhooks and system_events).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "c7d2f4a9b1e0"
down_revision = "54be551fc505"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")


def downgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("event_types", JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhooks")),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("webhook_id", UUID(as_uuid=True), nullable=False),
        sa.Column("system_event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhooks.id"],
            name=op.f("fk_webhook_deliveries_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["system_event_id"],
            ["system_events.id"],
            name=op.f("fk_webhook_deliveries_system_event_id_system_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint(
            "webhook_id", "system_event_id", name="uq_webhook_deliveries_webhook_system_event"
        ),
    )
    op.create_index(
        op.f("ix_webhook_deliveries_event_type"), "webhook_deliveries", ["event_type"], unique=False
    )
    op.create_index(
        op.f("ix_webhook_deliveries_system_event_id"), "webhook_deliveries", ["system_event_id"], unique=False
    )
    op.create_index(
        "ix_webhook_deliveries_status_next_retry_at",
        "webhook_deliveries",
        ["status", "next_retry_at"],
        unique=False,
    )
