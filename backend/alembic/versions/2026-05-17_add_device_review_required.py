"""add device review_required flag

Promotes the "device is shelved, needs operator attention" state into a
first-class column on ``devices``. Auto-recovery loops skip devices where
``review_required`` is True, replacing the misuse of
``health_failure:recovery`` RECOVERY-axis intents that previously
encoded the same idea but lived forever once registered.

Revision ID: 2c7d5a3e9f01
Revises: fb5b9341a7a3
Create Date: 2026-05-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2c7d5a3e9f01"
down_revision: str | None = "fb5b9341a7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "devices",
        sa.Column("review_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("review_set_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "review_set_at")
    op.drop_column("devices", "review_reason")
    op.drop_column("devices", "review_required")
