"""add partial index serving the live-session scans

The hot session queries all filter the *live* set — ``ended_at IS NULL`` with a
``status`` of ``running`` (the /routes rebuild, the liveness sweep, mark_ended /
activity) or ``running``/``pending`` (the shared ``live_session_predicate``:
state derivation, recovery/auto-stop gates, fleet capacity, run-release). The
only existing partial index leads on ``session_id`` (``ux_sessions_session_id_running``),
useless for these scans, so they seq-scan the full historical ``sessions`` table.

``ix_sessions_live`` is partial on ``ended_at IS NULL`` (live rows are a tiny
fraction of the historical table) and keys on ``(device_id, status)``:

* device-scoped existence checks (``live_session_predicate(device_id)``) use the
  ``device_id`` prefix plus the in-index ``status``;
* the un-filtered ``status``-only scans (/routes, liveness) become an index scan
  over the small partial index instead of a full-table seq-scan.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_sessions_live",
        "sessions",
        ["device_id", "status"],
        unique=False,
        postgresql_where="ended_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_live", table_name="sessions")
