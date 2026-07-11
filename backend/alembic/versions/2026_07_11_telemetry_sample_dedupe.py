"""Deduplicate host resource samples before enforcing replay-safe inserts."""

from __future__ import annotations

from alembic import op

revision = "20260711_telemetry_dedupe"
down_revision = "20260711_node_failing_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM host_resource_samples a USING host_resource_samples b "
        "WHERE a.id > b.id AND a.host_id = b.host_id AND a.recorded_at = b.recorded_at"
    )
    op.create_unique_constraint(
        "uq_host_resource_samples_host_recorded",
        "host_resource_samples",
        ["host_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_host_resource_samples_host_recorded",
        "host_resource_samples",
        type_="unique",
    )
