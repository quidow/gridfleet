"""drop devices.hold column and devicehold enum

Revision ID: aabb11223344
Revises: eeff55667788
Create Date: 2026-06-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aabb11223344"
down_revision: str | None = "eeff55667788"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("devices", "hold")
    op.execute("DROP TYPE IF EXISTS devicehold")


def downgrade() -> None:
    op.execute("CREATE TYPE devicehold AS ENUM ('maintenance', 'reserved')")
    op.add_column(
        "devices",
        sa.Column("hold", sa.Enum("maintenance", "reserved", name="devicehold"), nullable=True),
    )
