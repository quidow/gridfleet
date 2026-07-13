"""add sessions started_at,id index for list endpoints

Revision ID: 74233793ee45
Revises: 3bceace4dd7e
Create Date: 2026-07-13 16:36:30.839382

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '74233793ee45'
down_revision: Union[str, None] = '3bceace4dd7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Serves list_sessions / list_sessions_cursor: ORDER BY started_at DESC, id DESC
    # + keyset pagination with no device_id. Without it those queries seq-scan and
    # top-N sort the whole sessions table on every dashboard poll.
    #
    # Non-concurrent + IF NOT EXISTS: a no-op on any environment where the index
    # was already applied out-of-band; elsewhere it builds under a brief lock
    # (fast on any realistic sessions table). CONCURRENTLY is intentionally
    # avoided -- the control-plane leader holds a long-lived advisory-lock
    # transaction that would make a CONCURRENTLY build wait forever.
    op.create_index(
        "ix_sessions_started_at_id",
        "sessions",
        [sa.text("started_at DESC"), sa.text("id DESC")],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_started_at_id", table_name="sessions", if_exists=True)
