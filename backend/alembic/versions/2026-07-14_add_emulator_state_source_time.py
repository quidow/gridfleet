"""add emulator_state_source_time (M2 ordering)

Revision ID: e2c93f7a1b58
Revises: d5b81a6f0c94
Create Date: 2026-07-14 16:30:00.000000
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "e2c93f7a1b58"
down_revision: str | None = "d5b81a6f0c94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("emulator_state_source_time", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "emulator_state_source_time")
