"""Drop presentation/plumbing settings rows demoted to constants (WS-10.1)."""

from alembic import op

revision = "2026_07_11_drop_presentation"
down_revision = "20260711_telemetry_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN ("
        "'appium.session_override',"
        "'agent.default_port',"
        "'general.host_resource_telemetry_window_minutes',"
        "'notifications.toast_events',"
        "'notifications.toast_auto_dismiss_sec',"
        "'notifications.toast_severity_threshold')"
    )


def downgrade() -> None:
    pass  # The registry re-seeds defaults if the definitions are restored.
