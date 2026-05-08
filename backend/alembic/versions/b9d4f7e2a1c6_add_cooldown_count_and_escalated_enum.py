"""add cooldown_count column and lifecycle_run_cooldown_escalated enum value

Revision ID: b9d4f7e2a1c6
Revises: a7c9d2e4f6b8
Create Date: 2026-05-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9d4f7e2a1c6"
down_revision: str | None = "a7c9d2e4f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "device_reservations",
        sa.Column(
            "cooldown_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'lifecycle_run_cooldown_escalated'")


def downgrade() -> None:
    op.drop_column("device_reservations", "cooldown_count")
    # Postgres has no `ALTER TYPE ... DROP VALUE`; leaving the enum value in
    # place is consistent with project policy for additive enum migrations.
