"""add repair_attempted/repair_failed device event types

The connectivity loop records these audit rows when it dispatches an
adapter-recommended link-repair action (or exhausts the attempt budget).
Postgres enum values must be declared in a migration or migrated DBs 500 on
insert; pytest's create_all would not catch the gap (see
test_enum_migration_parity).

Revision ID: b8c9d0e1f2a3
Revises: f7a8b9c0d1e2
Create Date: 2026-06-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'repair_attempted'")
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'repair_failed'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values; the added labels are left in place.
    pass
