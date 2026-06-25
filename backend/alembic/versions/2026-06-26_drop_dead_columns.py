"""drop dead columns flagged by schema audit

Revision ID: a1c3e5f70d92
Revises: 54d7ea8b93a1
Create Date: 2026-06-26

Drop columns that were write-only or entirely unused (no read site in
application code, not exposed in any response schema):

- appium_nodes.grid_run_id        — superseded by desired_grid_run_id
- device_group_memberships.added_at
- host_runtime_installations.refcount — refcount is owned agent-side
- device_intent_dirty.reason       — written by mark_dirty, never read back
- driver_packs.origin              — constant 'uploaded' sentinel, never read
                                      (+ driver_packs_origin_ck check constraint)
- driver_packs.created_at / updated_at — never read (not in PackOut)
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "a1c3e5f70d92"
down_revision = "54d7ea8b93a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("driver_packs_origin_ck", "driver_packs", type_="check")
    op.drop_column("driver_packs", "origin")
    op.drop_column("driver_packs", "created_at")
    op.drop_column("driver_packs", "updated_at")
    op.drop_column("appium_nodes", "grid_run_id")
    op.drop_column("device_group_memberships", "added_at")
    op.drop_column("host_runtime_installations", "refcount")
    op.drop_column("device_intent_dirty", "reason")


def downgrade() -> None:
    op.add_column(
        "device_intent_dirty",
        sa.Column("reason", sa.String(), nullable=True),
    )
    op.add_column(
        "host_runtime_installations",
        sa.Column("refcount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "device_group_memberships",
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("grid_run_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "driver_packs",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "driver_packs",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "driver_packs",
        sa.Column("origin", sa.String(), nullable=False, server_default="uploaded"),
    )
    op.create_check_constraint("driver_packs_origin_ck", "driver_packs", "origin = 'uploaded'")
