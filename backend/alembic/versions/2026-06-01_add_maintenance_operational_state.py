"""add maintenance to deviceoperationalstate

Revision ID: aabb1122ccdd
Revises: 08a430a9653e
Create Date: 2026-06-01

Postgres forbids using a new enum value in the same transaction that adds it, so this
migration ONLY adds the value. The backfill that *uses* it is a separate revision.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "aabb1122ccdd"
down_revision: str | None = "08a430a9653e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceoperationalstate ADD VALUE IF NOT EXISTS 'maintenance'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; downgrade is intentionally a no-op.
    pass
