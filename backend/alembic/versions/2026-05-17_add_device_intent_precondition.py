"""add device_intents.precondition

Adds a nullable JSONB column on ``device_intents`` so each intent can carry
a declarative precondition. The reconciler evaluates it each tick and
deletes rows whose precondition no longer holds, replacing the implicit
"manual revoke at every site that drives the underlying state change"
pattern with a single sweeper.

Revision ID: 3e9a8d11c7b2
Revises: 2c7d5a3e9f01
Create Date: 2026-05-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "3e9a8d11c7b2"
down_revision: str | None = "2c7d5a3e9f01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "device_intents",
        sa.Column("precondition", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_intents", "precondition")
