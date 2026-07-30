"""drop device review shelving columns

Revision ID: 6deb651ca312
Revises: 20260729_pack_artifacts
Create Date: 2026-07-30 16:43:06.202167

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6deb651ca312"
down_revision: str | None = "20260729_pack_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("devices", "review_set_at")
    op.drop_column("devices", "review_reason")
    op.drop_column("devices", "review_required")


def downgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("devices", sa.Column("review_reason", sa.Text(), nullable=True))
    op.add_column("devices", sa.Column("review_set_at", sa.DateTime(timezone=True), nullable=True))
