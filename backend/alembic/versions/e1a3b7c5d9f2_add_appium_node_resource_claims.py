"""add appium_node_resource_claims

Revision ID: e1a3b7c5d9f2
Revises: d4f2a8c1b3e7
Create Date: 2026-05-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e1a3b7c5d9f2"
down_revision = "d4f2a8c1b3e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appium_node_resource_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_key", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appium_nodes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("owner_token", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("host_id", "capability_key", "port", name="uq_appium_node_resource_claims_port"),
        sa.CheckConstraint(
            "(node_id IS NOT NULL AND owner_token IS NULL AND expires_at IS NULL) "
            "OR (node_id IS NULL AND owner_token IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_appium_node_resource_claims_flavour",
        ),
        sa.Index("ix_appium_node_resource_claims_node_id", "node_id"),
        sa.Index("ix_appium_node_resource_claims_owner_token", "host_id", "owner_token"),
        sa.Index("ix_appium_node_resource_claims_expires_at", "expires_at"),
    )
    op.create_index(
        "uq_appium_node_resource_claims_temp_owner",
        "appium_node_resource_claims",
        ["host_id", "owner_token", "capability_key"],
        unique=True,
        postgresql_where=sa.text("owner_token IS NOT NULL"),
    )
    op.create_index(
        "uq_appium_node_resource_claims_managed_node",
        "appium_node_resource_claims",
        ["node_id", "capability_key"],
        unique=True,
        postgresql_where=sa.text("node_id IS NOT NULL"),
    )
    op.add_column(
        "appium_nodes",
        sa.Column(
            "live_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        "DELETE FROM control_plane_state_entries "
        "WHERE namespace = 'appium.parallel.owner' "
        "OR namespace LIKE 'appium.parallel.claim.%'"
    )


def downgrade() -> None:
    op.drop_column("appium_nodes", "live_capabilities")
    op.drop_table("appium_node_resource_claims")
