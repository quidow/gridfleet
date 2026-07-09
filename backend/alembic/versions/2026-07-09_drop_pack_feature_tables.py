"""drop pack feature tables

Revision ID: d4dfbec2564a
Revises: 49e8065414a1
Create Date: 2026-07-09 11:52:22.193249

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4dfbec2564a'
down_revision: Union[str, None] = 'c3f1a2b4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("host_pack_feature_status")
    op.drop_table("driver_pack_features")


def downgrade() -> None:
    # Recreate the tables as they stood at the prior revision. The single-column
    # host_id index was already dropped upstream (drop_redundant_single_column_indexes),
    # so it is intentionally not recreated here.
    op.create_table(
        "host_pack_feature_status",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("host_id", sa.UUID(), nullable=False),
        sa.Column("pack_id", sa.String(), nullable=False),
        sa.Column("feature_id", sa.String(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.String(), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "pack_id", "feature_id", name="host_pack_feature_status_uq"),
    )
    op.create_table(
        "driver_pack_features",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("pack_release_id", sa.UUID(), nullable=False),
        sa.Column("manifest_feature_id", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["pack_release_id"], ["driver_pack_releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pack_release_id", "manifest_feature_id", name="driver_pack_features_release_feature_uq"),
    )
