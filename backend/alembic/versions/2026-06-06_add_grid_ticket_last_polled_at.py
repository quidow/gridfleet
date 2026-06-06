"""add grid_session_queue.last_polled_at for ticket liveness

A waiting ticket whose client has half-closed (the router's allocate long-poll
cannot detect a dead downstream) otherwise FIFO-vetoes every younger waiter for
up to ``grid.queue_timeout_sec``. ``last_polled_at`` is stamped on every poll of
``try_allocate`` so the FIFO veto and the reaper can treat a ticket not re-polled
within a few poll intervals as dead. ``updated_at`` cannot serve this: its
``onupdate`` also fires on status transitions, conflating "client still polling"
with "status changed", and a no-op write would not bump it at all.

Backfills existing waiting tickets to ``now()`` so a deploy does not instantly
expire in-flight waiters.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "grid_session_queue",
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill in-flight waiters so the staleness reaper does not expire them on the
    # first tick after deploy.
    op.execute("UPDATE grid_session_queue SET last_polled_at = now() WHERE status = 'waiting'")


def downgrade() -> None:
    op.drop_column("grid_session_queue", "last_polled_at")
