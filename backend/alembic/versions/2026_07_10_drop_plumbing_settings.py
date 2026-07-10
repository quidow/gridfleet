"""drop plumbing settings replaced by code constants (ws-4.2)

Revision ID: 20260710_drop_plumbing
Revises: 2026_07_10_drop_loop_settings
"""

from alembic import op

revision = "20260710_drop_plumbing"
down_revision = "2026_07_10_drop_loop_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'general.heartbeat_interval_sec',"
        "'general.partition_probe_interval_sec',"
        "'general.intent_reconcile_interval_sec',"
        "'grid.session_poll_interval_sec',"
        "'appium_reconciler.host_parallelism',"
        "'agent.http_pool_enabled',"
        "'agent.http_pool_max_keepalive',"
        "'agent.http_pool_idle_seconds',"
        "'agent.circuit_breaker_failure_threshold',"
        "'agent.circuit_breaker_cooldown_seconds')"
    )


def downgrade() -> None:
    pass
