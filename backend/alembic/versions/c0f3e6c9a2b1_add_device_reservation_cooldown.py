"""add device reservation cooldown

Revision ID: c0f3e6c9a2b1
Revises: 9c1f4d8b6e2a
Create Date: 2026-05-03 20:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0f3e6c9a2b1"
down_revision: Union[str, None] = "9c1f4d8b6e2a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_reservations",
        sa.Column("excluded_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_device_reservations_cooldown_active",
        "device_reservations",
        ["run_id", "device_id", "excluded_until"],
        unique=False,
        postgresql_where=sa.text("excluded_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_device_reservations_cooldown_active", table_name="device_reservations")
    op.drop_column("device_reservations", "excluded_until")
