"""add pending session status + last_activity_at

Revision ID: 3fe8a129e081
Revises: aabb11223344
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3fe8a129e081"
down_revision: str | None = "aabb11223344"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres 12+ allows ADD VALUE inside a transaction block as long as the
    # new value is not used in the same transaction (it is not).
    op.execute("ALTER TYPE sessionstatus ADD VALUE IF NOT EXISTS 'pending'")
    op.add_column("sessions", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Postgres cannot drop enum values; pending rows are transient (claim window) so
    # downgrade only removes the column.
    op.drop_column("sessions", "last_activity_at")
