"""add node health fold receipts

Revision ID: c7a94b1e2d63
Revises: b2e4d0518c71
Create Date: 2026-07-14 14:30:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c7a94b1e2d63"
down_revision: str | None = "b2e4d0518c71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appium_nodes",
        sa.Column("health_fold_applied_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("health_fold_boot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "appium_nodes",
        sa.Column("health_fold_section_sequence", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appium_nodes", "health_fold_section_sequence")
    op.drop_column("appium_nodes", "health_fold_boot_id")
    op.drop_column("appium_nodes", "health_fold_applied_revision")
