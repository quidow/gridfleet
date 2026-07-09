"""comms 6b: recency-based host liveness — drop max_missed_heartbeats and legacy heartbeat state.

Revision ID: 2026_07_09_comms_status_push
Revises: 2026_07_09_restart_watermark
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op

revision: str = "2026_07_09_comms_status_push"
down_revision: str | None = "2026_07_09_restart_watermark"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'general.max_missed_heartbeats'")
    op.execute(
        "DELETE FROM control_plane_state_entries "
        "WHERE namespace IN ('heartbeat.failure_count', 'heartbeat.appium_processes')"
    )


def downgrade() -> None:
    pass  # the registry re-seeds the default if the definition is restored; state rows rebuild organically
