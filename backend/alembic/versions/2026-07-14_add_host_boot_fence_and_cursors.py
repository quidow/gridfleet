"""add host boot fence and observation cursors

Revision ID: b2e4d0518c71
Revises: a1f3c9d27b60
Create Date: 2026-07-14 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2e4d0518c71'
down_revision: Union[str, None] = 'a1f3c9d27b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("current_boot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "observation_cursors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "observation_applied",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("hosts", "observation_applied")
    op.drop_column("hosts", "observation_cursors")
    op.drop_column("hosts", "current_boot_id")
