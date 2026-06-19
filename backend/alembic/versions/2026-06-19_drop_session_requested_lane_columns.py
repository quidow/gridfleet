"""drop session requested_* lane columns (Selenium-Grid register_session vestige)

Revision ID: e765e30647c4
Revises: 7d0a5cd47850
Create Date: 2026-06-19

These four columns were written only by the removed client-side register_session
endpoint (PR #620). NULL on every row created since. requested_capabilities is kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e765e30647c4"
down_revision: str | None = "7d0a5cd47850"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("sessions", "requested_pack_id")
    op.drop_column("sessions", "requested_platform_id")
    op.drop_column("sessions", "requested_device_type")
    op.drop_column("sessions", "requested_connection_type")


def downgrade() -> None:
    op.add_column("sessions", sa.Column("requested_pack_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("requested_platform_id", sa.String(), nullable=True))
    # devicetype / connectiontype enums already exist (used by devices.*); reference, don't recreate.
    op.add_column(
        "sessions",
        sa.Column("requested_device_type", sa.Enum(name="devicetype", create_type=False), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("requested_connection_type", sa.Enum(name="connectiontype", create_type=False), nullable=True),
    )
