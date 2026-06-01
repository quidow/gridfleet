"""backfill operational_state=maintenance where hold=maintenance

Revision ID: eeff55667788
Revises: aabb1122ccdd
Create Date: 2026-06-02
"""
from collections.abc import Sequence

from alembic import op

revision: str = "eeff55667788"
down_revision: str | None = "aabb1122ccdd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE devices SET operational_state = 'maintenance' "
        "WHERE hold = 'maintenance' AND operational_state <> 'maintenance'"
    )


def downgrade() -> None:
    # The reconciler re-derives state continuously; a precise inverse is not meaningful.
    pass
