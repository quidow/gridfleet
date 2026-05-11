"""add verifying state and drop temp resource claims

Revision ID: bb1160a9697a
Revises: 75ebf4939df8
Create Date: 2026-05-11 19:21:12.799295

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb1160a9697a"
down_revision: str | None = "75ebf4939df8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceoperationalstate ADD VALUE IF NOT EXISTS 'verifying'")

    op.execute("DELETE FROM appium_node_resource_claims WHERE node_id IS NULL")

    op.drop_index("uq_appium_node_resource_claims_temp_owner", table_name="appium_node_resource_claims")
    op.drop_index("ix_appium_node_resource_claims_owner_token", table_name="appium_node_resource_claims")
    op.drop_index("ix_appium_node_resource_claims_expires_at", table_name="appium_node_resource_claims")
    op.drop_constraint(
        "ck_appium_node_resource_claims_flavour",
        "appium_node_resource_claims",
        type_="check",
    )
    op.alter_column(
        "appium_node_resource_claims",
        "node_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("appium_node_resource_claims", "owner_token")
    op.drop_column("appium_node_resource_claims", "expires_at")


def downgrade() -> None:
    op.add_column("appium_node_resource_claims", sa.Column("owner_token", sa.String(), nullable=True))
    op.add_column("appium_node_resource_claims", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "appium_node_resource_claims",
        "node_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_appium_node_resource_claims_flavour",
        "appium_node_resource_claims",
        "(node_id IS NOT NULL AND owner_token IS NULL AND expires_at IS NULL) "
        "OR (node_id IS NULL AND owner_token IS NOT NULL AND expires_at IS NOT NULL)",
    )
    op.create_index(
        "ix_appium_node_resource_claims_owner_token",
        "appium_node_resource_claims",
        ["host_id", "owner_token"],
    )
    op.create_index(
        "ix_appium_node_resource_claims_expires_at",
        "appium_node_resource_claims",
        ["expires_at"],
    )
    op.create_index(
        "uq_appium_node_resource_claims_temp_owner",
        "appium_node_resource_claims",
        ["host_id", "owner_token", "capability_key"],
        unique=True,
        postgresql_where=sa.text("owner_token IS NOT NULL"),
    )
    # PostgreSQL cannot drop enum values without recreating the type, so the
    # 'verifying' value intentionally remains on downgrade.
