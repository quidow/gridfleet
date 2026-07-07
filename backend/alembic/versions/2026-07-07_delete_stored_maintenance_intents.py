"""delete stored maintenance intents

Revision ID: 40b7d387a9c1
Revises: 550134190745
Create Date: 2026-07-07 19:20:13.863782

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "40b7d387a9c1"
down_revision: str | None = "550134190745"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # maintenance:node / maintenance:recovery intents are now synthesized from the
    # maintenance_reason fact.
    op.execute("DELETE FROM device_intents WHERE source LIKE 'maintenance:%'")


def downgrade() -> None:
    pass  # rows are re-derivable; nothing to restore
