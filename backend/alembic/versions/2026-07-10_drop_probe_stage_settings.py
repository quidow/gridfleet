"""drop probe stage settings

Revision ID: 2026_07_10_drop_probe_settings
Revises: 2026_07_10_intent_kind
"""

from alembic import op

revision = "2026_07_10_drop_probe_settings"
down_revision = "2026_07_10_intent_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'general.node_check_interval_sec',"
        "'general.device_check_interval_sec',"
        "'general.host_resource_telemetry_interval_sec',"
        "'general.hardware_telemetry_interval_sec',"
        "'general.property_refresh_interval_sec',"
        "'general.probe_concurrency_per_host')"
    )


def downgrade() -> None:
    pass  # The registry re-seeds defaults if the definitions are restored.
