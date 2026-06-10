"""drop ten standalone indexes fully shadowed by composite/unique indexes

Each dropped index's column is the leading column of another non-partial
index on the same table, so every scan it could serve is still served;
the standalone copy was pure write amplification (same rationale as the
ix_grid_session_queue_status drop). device_reservations_device_id_idx and
sessions_session_id_idx are intentionally kept: their would-be shadows are
partial indexes.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED: tuple[tuple[str, str, list[str]], ...] = (
    ("jobs_kind_idx", "jobs", ["kind"]),
    ("system_events_type_idx", "system_events", ["type"]),
    ("webhook_deliveries_status_idx", "webhook_deliveries", ["status"]),
    ("webhook_deliveries_webhook_id_idx", "webhook_deliveries", ["webhook_id"]),
    ("device_events_device_id_idx", "device_events", ["device_id"]),
    ("agent_reconfigure_outbox_device_id_idx", "agent_reconfigure_outbox", ["device_id"]),
    ("device_intents_device_id_idx", "device_intents", ["device_id"]),
    ("host_pack_feature_status_host_id_idx", "host_pack_feature_status", ["host_id"]),
    ("host_agent_log_entry_host_id_idx", "host_agent_log_entry", ["host_id"]),
    ("control_plane_state_entries_namespace_idx", "control_plane_state_entries", ["namespace"]),
)


def upgrade() -> None:
    for index_name, table_name, _columns in _DROPPED:
        op.drop_index(index_name, table_name=table_name)


def downgrade() -> None:
    for index_name, table_name, columns in reversed(_DROPPED):
        op.create_index(index_name, table_name, columns)
