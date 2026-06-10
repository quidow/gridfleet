"""add indexes on four FK columns whose parents are deleted routinely

Without these, every sessions prune (data_cleanup), device delete, or
driver-pack delete seq-scans the child table to fire SET NULL / CASCADE.
Index names follow the project naming convention (%(column_0_label)s_idx).

Revision ID: b1c2d3e4f5a6
Revises: a3b4c5d6e7f8
Create Date: 2026-06-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("grid_session_queue_session_row_id_idx", "grid_session_queue", ["session_row_id"])
    op.create_index("device_group_memberships_device_id_idx", "device_group_memberships", ["device_id"])
    op.create_index("host_pack_installations_pack_id_idx", "host_pack_installations", ["pack_id"])
    op.create_index("host_pack_doctor_results_pack_id_idx", "host_pack_doctor_results", ["pack_id"])


def downgrade() -> None:
    op.drop_index("host_pack_doctor_results_pack_id_idx", table_name="host_pack_doctor_results")
    op.drop_index("host_pack_installations_pack_id_idx", table_name="host_pack_installations")
    op.drop_index("device_group_memberships_device_id_idx", table_name="device_group_memberships")
    op.drop_index("grid_session_queue_session_row_id_idx", table_name="grid_session_queue")
