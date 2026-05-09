"""unique running session_id

Collapse duplicate ``running`` Session rows that share a ``session_id`` and
add a partial unique index so concurrent insert paths (testkit
POST /sessions, session_sync._sync_sessions) cannot diverge again.

Revision ID: a7b4c1f9d3e8
Revises: e49bb9325f8a
Create Date: 2026-05-09 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b4c1f9d3e8"
down_revision: str | None = "e49bb9325f8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # End all-but-one duplicate ``running`` rows per ``session_id``. Keep the
    # most recently started row as the canonical record; the others are
    # closed out as ``error`` with an explanatory error_type so operators can
    # audit how the divergence happened. Without this collapse the partial
    # unique index below would fail to create on instances that already hit
    # the bug.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY started_at DESC, id DESC
                    ) AS rn
                FROM sessions
                WHERE status = 'running' AND ended_at IS NULL
            )
            UPDATE sessions
            SET status = 'error',
                ended_at = NOW(),
                error_type = COALESCE(error_type, 'duplicate_running'),
                error_message = COALESCE(
                    error_message,
                    'Closed by migration a7b4c1f9d3e8: duplicate running row for session_id'
                )
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        )
    )

    op.create_index(
        "ux_sessions_session_id_running",
        "sessions",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_sessions_session_id_running", table_name="sessions")
