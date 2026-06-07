"""add sessions.actual_capabilities

Negotiated capabilities from the Appium create-session response, captured by
the router at confirm time. NULL for pre-feature rows and non-router sessions.

Revision ID: a3b4c5d6e7f8
Revises: f7a8b9c0d1e2
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("actual_capabilities", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "actual_capabilities")
