"""delete stored run routing intents

Revision ID: 550134190745
Revises: 86856afd09bb
Create Date: 2026-07-07 19:02:16.958918

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "550134190745"
down_revision: str | None = "86856afd09bb"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # run: grid-routing intents are now synthesized from the reservation row.
    op.execute("DELETE FROM device_intents WHERE source LIKE 'run:%'")


def downgrade() -> None:
    pass  # rows are re-derivable; nothing to restore
