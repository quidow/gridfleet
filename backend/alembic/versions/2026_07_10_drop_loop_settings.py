"""drop janitor-merged loop interval settings

Revision ID: 2026_07_10_drop_loop_settings
Revises: 2026_07_10_drop_probe_settings
"""

from alembic import op

revision = "2026_07_10_drop_loop_settings"
down_revision = "2026_07_10_drop_probe_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'general.fleet_capacity_snapshot_interval_sec',"
        "'general.background_loop_flush_interval_sec',"
        "'reservations.reaper_interval_sec',"
        "'retention.cleanup_interval_hours')"
    )


def downgrade() -> None:
    pass  # The registry re-seeds defaults if the definitions are restored.
