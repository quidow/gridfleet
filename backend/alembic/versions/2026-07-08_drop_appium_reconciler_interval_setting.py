"""drop appium reconciler interval setting

Revision ID: 7b4e8d1f2a6c
Revises: 9053c8d3caa2
Create Date: 2026-07-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7b4e8d1f2a6c"
down_revision: str | None = "9053c8d3caa2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'appium_reconciler.interval_sec'")


def downgrade() -> None:
    pass  # The registry re-seeds the default if the definition is restored.
