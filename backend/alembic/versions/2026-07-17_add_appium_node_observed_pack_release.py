"""add appium node observed_pack_release.

Revision ID: 9a81a1015b09
Revises: f3a7c1b920de
Create Date: 2026-07-17 14:23:47.209157

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "9a81a1015b09"
down_revision: str | None = "f3a7c1b920de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appium_nodes", sa.Column("observed_pack_release", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("appium_nodes", "observed_pack_release")
