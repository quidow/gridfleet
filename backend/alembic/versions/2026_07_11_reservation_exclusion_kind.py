"""add exclusion_kind to device_reservations

Revision ID: 2026_07_11_exclusion_kind
Revises: aeea5f241bcb
Create Date: 2026-07-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2026_07_11_exclusion_kind'
down_revision: Union[str, None] = 'aeea5f241bcb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("device_reservations", sa.Column("exclusion_kind", sa.String(length=16), nullable=True))
    # Backfill from the NULL-ness encoding this column replaces. Released rows are
    # never excluded (release invariant), so they backfill to NULL naturally; an
    # expired-but-unswept cooldown correctly backfills as 'cooldown'.
    op.execute(
        "UPDATE device_reservations SET exclusion_kind = CASE "
        "WHEN excluded AND excluded_until IS NOT NULL THEN 'cooldown' "
        "WHEN excluded THEN 'exclusion' "
        "ELSE NULL END"
    )


def downgrade() -> None:
    op.drop_column("device_reservations", "exclusion_kind")
