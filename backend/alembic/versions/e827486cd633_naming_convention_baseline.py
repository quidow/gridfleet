"""naming-convention-baseline

Revision ID: e827486cd633
Revises: 53063d67b14c
Create Date: 2026-05-15 14:23:43.756998

Renames legacy default index/constraint names to the project naming
convention. Idempotent: skips renames when the source object is absent,
so fresh databases (which already build with the new names via
``Base.metadata`` naming_convention in ``op.create_table``) and migrated
existing databases both reach the same end state.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e827486cd633"
down_revision: str | None = "53063d67b14c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_RENAMES: tuple[tuple[str, str], ...] = (
    ("ix_agent_reconfigure_outbox_device_id", "agent_reconfigure_outbox_device_id_idx"),
    ("ix_appium_nodes_desired_grid_run_id", "appium_nodes_desired_grid_run_id_idx"),
    ("ix_appium_plugins_name", "appium_plugins_name_idx"),
    ("ix_config_audit_logs_device_id", "config_audit_logs_device_id_idx"),
    ("ix_control_plane_state_entries_key", "control_plane_state_entries_key_idx"),
    ("ix_control_plane_state_entries_namespace", "control_plane_state_entries_namespace_idx"),
    ("ix_device_events_device_id", "device_events_device_id_idx"),
    ("ix_device_intents_axis", "device_intents_axis_idx"),
    ("ix_device_intents_device_id", "device_intents_device_id_idx"),
    ("ix_device_intents_expires_at", "device_intents_expires_at_idx"),
    ("ix_device_intents_run_id", "device_intents_run_id_idx"),
    ("ix_device_intents_source", "device_intents_source_idx"),
    ("ix_device_reservations_device_id", "device_reservations_device_id_idx"),
    ("ix_device_reservations_run_id", "device_reservations_run_id_idx"),
    ("ix_device_test_data_audit_logs_device_id", "device_test_data_audit_logs_device_id_idx"),
    ("ix_devices_identity_value", "devices_identity_value_idx"),
    ("ix_host_pack_feature_status_host_id", "host_pack_feature_status_host_id_idx"),
    ("ix_host_terminal_sessions_host_id", "host_terminal_sessions_host_id_idx"),
    ("ix_hosts_hostname", "hosts_hostname_idx"),
    ("ix_jobs_kind", "jobs_kind_idx"),
    ("ix_jobs_scheduled_at", "jobs_scheduled_at_idx"),
    ("ix_jobs_status", "jobs_status_idx"),
    ("ix_sessions_run_id", "sessions_run_id_idx"),
    ("ix_sessions_session_id", "sessions_session_id_idx"),
    ("ix_settings_key", "settings_key_idx"),
    ("ix_system_events_event_id", "system_events_event_id_idx"),
    ("ix_system_events_type", "system_events_type_idx"),
    ("ix_webhook_deliveries_event_type", "webhook_deliveries_event_type_idx"),
    ("ix_webhook_deliveries_status", "webhook_deliveries_status_idx"),
    ("ix_webhook_deliveries_system_event_id", "webhook_deliveries_system_event_id_idx"),
    ("ix_webhook_deliveries_webhook_id", "webhook_deliveries_webhook_id_idx"),
)


CONSTRAINT_RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "device_group_memberships",
        "device_group_memberships_group_id_device_id_key",
        "device_group_memberships_group_id_key",
    ),
    (
        "appium_nodes",
        "ck_appium_nodes_desired_state",
        "appium_nodes_desired_state_check",
    ),
    (
        "appium_nodes",
        "ck_appium_nodes_desired_port_requires_running",
        "appium_nodes_desired_port_requires_running_check",
    ),
)


def _rename_index_if_exists(old: str, new: str) -> None:
    op.execute(sa.text(f'ALTER INDEX IF EXISTS "{old}" RENAME TO "{new}"'))


def _rename_constraint_if_exists(table: str, old: str, new: str) -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS ("
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            f"WHERE c.conname = '{old}' AND t.relname = '{table}' "
            "AND n.nspname = current_schema()"
            ") THEN "
            f'EXECUTE \'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"\'; '
            "END IF; END $$"
        )
    )


def upgrade() -> None:
    for old, new in INDEX_RENAMES:
        _rename_index_if_exists(old, new)
    for table, old, new in CONSTRAINT_RENAMES:
        _rename_constraint_if_exists(table, old, new)


def downgrade() -> None:
    for table, old, new in CONSTRAINT_RENAMES:
        _rename_constraint_if_exists(table, new, old)
    for old, new in INDEX_RENAMES:
        _rename_index_if_exists(new, old)
