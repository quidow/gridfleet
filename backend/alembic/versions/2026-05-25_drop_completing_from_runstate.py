"""drop completing from runstate

Revision ID: 08a430a9653e
Revises: f1de2fce530c
Create Date: 2026-05-25 22:14:47.289676

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "08a430a9653e"
down_revision: str | None = "f1de2fce530c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("pending", "preparing", "active", "completing", "completed", "failed", "expired", "cancelled")
_NEW_VALUES = ("pending", "preparing", "active", "completed", "failed", "expired", "cancelled")


def upgrade() -> None:
    op.execute("ALTER TYPE runstate RENAME TO runstate_old")
    op.execute(f"CREATE TYPE runstate AS ENUM {_NEW_VALUES!r}")
    op.execute("ALTER TABLE test_runs ALTER COLUMN state TYPE runstate USING state::text::runstate")
    op.execute("DROP TYPE runstate_old")


def downgrade() -> None:
    op.execute("ALTER TYPE runstate RENAME TO runstate_old")
    op.execute(f"CREATE TYPE runstate AS ENUM {_OLD_VALUES!r}")
    op.execute("ALTER TABLE test_runs ALTER COLUMN state TYPE runstate USING state::text::runstate")
    op.execute("DROP TYPE runstate_old")
