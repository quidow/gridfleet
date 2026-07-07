"""delete reservation axis intents

Revision ID: 2e620208b44e
Revises: 40b7d387a9c1
Create Date: 2026-07-07 19:40:00.000000

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e620208b44e"
down_revision: str | None = "40b7d387a9c1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The RESERVATION axis is deleted. cooldown:grid / cooldown:recovery are now
    # synthesized from the reservation row's excluded_until window; cooldown:reservation
    # and health_failure:reservation have no synthesized twin (the row is the state).
    op.execute(
        "DELETE FROM device_intents WHERE source LIKE 'cooldown:%' "
        "OR source LIKE 'health_failure:reservation:%'"
    )


def downgrade() -> None:
    pass  # rows are re-derivable / row-direct; nothing to restore
