"""drop the redundant standalone grid_session_queue.status index

``status`` is the leftmost column of the composite
``ix_grid_session_queue_status_created_at``, which serves every status-only
scan (the reaper's ``status = 'waiting' ORDER BY created_at`` and the FIFO
veto load). The standalone ``ix_grid_session_queue_status`` was pure write
amplification on a table churned every allocation poll (wave-5 review #14).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_grid_session_queue_status", table_name="grid_session_queue")


def downgrade() -> None:
    op.create_index("ix_grid_session_queue_status", "grid_session_queue", ["status"])
