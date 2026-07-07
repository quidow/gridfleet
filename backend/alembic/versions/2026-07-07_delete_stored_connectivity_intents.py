"""delete stored connectivity intents

Revision ID: 6a10f465d10d
Revises: 2e620208b44e
Create Date: 2026-07-07 20:05:00.000000

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a10f465d10d"
down_revision: str | None = "2e620208b44e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # connectivity: defer-stops are now synthesized from device_checks_healthy IS FALSE.
    op.execute("DELETE FROM device_intents WHERE source LIKE 'connectivity:%'")


def downgrade() -> None:
    pass  # rows are re-derivable; nothing to restore
