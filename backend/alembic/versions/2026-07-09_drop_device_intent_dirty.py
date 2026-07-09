"""drop device_intent_dirty and the full-scan cadence setting

The intent reconciler scans every device every tick; the dirty work queue
and general.intent_reconcile_full_scan_every_cycles are gone.

Revision ID: c3f1a2b4d5e6
Revises: 2f0e0d84a638
Create Date: 2026-07-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f1a2b4d5e6"
down_revision: str | None = "2f0e0d84a638"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("device_intent_dirty")
    op.execute("DELETE FROM settings WHERE key = 'general.intent_reconcile_full_scan_every_cycles'")


def downgrade() -> None:
    # Recreate the table in the shape it had at the down-revision: the `reason`
    # column was already dropped by 2026-06-26_drop_dead_columns, so it is NOT
    # restored here (that migration's own downgrade re-adds it).
    op.create_table(
        "device_intent_dirty",
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("dirty_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id"),
    )
    # The registry re-seeds general.intent_reconcile_full_scan_every_cycles if its
    # definition is restored.
