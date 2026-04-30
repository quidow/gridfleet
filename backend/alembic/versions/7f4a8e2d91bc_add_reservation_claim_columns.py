"""add reservation claim columns

Revision ID: 7f4a8e2d91bc
Revises: 2d8d5e5bc460
Create Date: 2026-04-30 17:45:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f4a8e2d91bc"
down_revision: Union[str, None] = "2d8d5e5bc460"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("device_reservations", sa.Column("claimed_by", sa.String(), nullable=True))
    op.add_column(
        "device_reservations",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_reservations", "claimed_at")
    op.drop_column("device_reservations", "claimed_by")
