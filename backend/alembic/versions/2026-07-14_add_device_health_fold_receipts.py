"""add device health fold receipts

Revision ID: d1e5f7a9c024
Revises: e2c93f7a1b58
Create Date: 2026-07-14 16:00:00.000000
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "d1e5f7a9c024"
down_revision: str | None = "e2c93f7a1b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("device_checks_fold_applied_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("devices", sa.Column("device_checks_fold_boot_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("devices", sa.Column("device_checks_fold_section_sequence", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "device_checks_fold_section_sequence")
    op.drop_column("devices", "device_checks_fold_boot_id")
    op.drop_column("devices", "device_checks_fold_applied_revision")
