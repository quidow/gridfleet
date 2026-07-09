"""Restart watermark replaces transition-token lease.

Revision ID: 2026_07_09_restart_watermark
Revises: 2026_07_09_drop_rec_shadow
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2026_07_09_restart_watermark"
down_revision: str | None = "2026_07_09_drop_rec_shadow"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("appium_nodes", sa.Column("restart_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("appium_nodes", "transition_token")
    op.drop_column("appium_nodes", "transition_deadline")
    op.drop_column("appium_nodes", "generation")
    op.drop_column("appium_nodes", "container_id")


def downgrade() -> None:
    op.add_column("appium_nodes", sa.Column("container_id", sa.String(), nullable=True))
    op.add_column("appium_nodes", sa.Column("generation", sa.Integer(), server_default="0", nullable=False))
    op.add_column("appium_nodes", sa.Column("transition_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("appium_nodes", sa.Column("transition_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.drop_column("appium_nodes", "restart_requested_at")
