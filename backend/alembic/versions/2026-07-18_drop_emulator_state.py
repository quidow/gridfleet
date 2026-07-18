"""drop emulator_state and emulator_state_source_time

Revision ID: 0f9be4f61f49
Revises: 9a81a1015b09
Create Date: 2026-07-18 13:09:47.040473

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0f9be4f61f49"
down_revision: str | None = "9a81a1015b09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("devices", "emulator_state")
    op.drop_column("devices", "emulator_state_source_time")


def downgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("emulator_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column(
            "emulator_state_source_time",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
