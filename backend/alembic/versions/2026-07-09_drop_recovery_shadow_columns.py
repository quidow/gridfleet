"""Drop recovery_allowed/recovery_blocked_reason — projected at read since this release.

Revision ID: 2026_07_09_drop_rec_shadow
Revises: d4dfbec2564a
Create Date: 2026-07-09

The recovery decision is now recomputed at read time by
app.devices.services.recovery_projection.recovery_availability; the cached
copy on the device row is no longer read by anything.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "2026_07_09_drop_rec_shadow"
down_revision: str | None = "d4dfbec2564a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("devices", "recovery_allowed")
    op.drop_column("devices", "recovery_blocked_reason")


def downgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("recovery_allowed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("devices", sa.Column("recovery_blocked_reason", sa.Text(), nullable=True))
