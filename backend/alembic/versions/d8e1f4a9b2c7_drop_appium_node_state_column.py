"""drop appium_nodes.state column

Revision ID: d8e1f4a9b2c7
Revises: c5f0d8e1a4b9
Create Date: 2026-05-10 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8e1f4a9b2c7"
down_revision: str | None = "c5f0d8e1a4b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("appium_nodes", "state")


def downgrade() -> None:
    op.add_column(
        "appium_nodes",
        sa.Column(
            "state",
            postgresql.ENUM(name="nodestate", create_type=False),
            nullable=True,
        ),
    )
