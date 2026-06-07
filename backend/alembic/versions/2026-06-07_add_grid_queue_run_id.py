"""add grid_session_queue.run_id for run-scoped endpoint binding

The router's /run/{run_id} WebDriver endpoint passes the run id as a
first-class allocate field; the ticket stores it here (NULL = free session).
No FK to test_runs: tickets are short-lived and validated against an active
run on every try_allocate tick — a vanished run cancels the ticket rather
than blocking run deletion.

Revision ID: f7a8b9c0d1e2
Revises: d4e5f6a7b8c9
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("grid_session_queue", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("grid_session_queue", "run_id")
