"""drop intent precondition and legacy rows

Revision ID: a991cd9df7b5
Revises: 6a10f465d10d
Create Date: 2026-07-07 20:46:12.148115

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a991cd9df7b5"
down_revision: str | None = "6a10f465d10d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Defensive-revoke-only sources that were never registered by current code,
    # plus any straggler rows from families converted in prior releases.
    op.execute(
        "DELETE FROM device_intents WHERE source LIKE 'active_session:%' "
        "OR source LIKE 'operator:stop:grid:%' OR source LIKE 'maintenance:grid:%' "
        "OR source LIKE 'cooldown:node:%' OR source LIKE 'health_failure:recovery:%'"
    )
    op.drop_column("device_intents", "precondition")


def downgrade() -> None:
    op.add_column("device_intents", sa.Column("precondition", postgresql.JSONB(), nullable=True))
