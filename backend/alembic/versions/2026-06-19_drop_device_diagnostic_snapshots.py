"""drop device diagnostic snapshots

Removes the device diagnostics feature: drops the snapshot table/index and
deletes the orphaned ``retention.diagnostic_snapshots_days`` settings row.

Revision ID: 79679b99101c
Revises: e765e30647c4
Create Date: 2026-06-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "79679b99101c"
down_revision: str | None = "e765e30647c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_device_diagnostic_snapshots_device_id_captured_at",
        table_name="device_diagnostic_snapshots",
    )
    op.drop_table("device_diagnostic_snapshots")
    op.execute("DELETE FROM settings WHERE key = 'retention.diagnostic_snapshots_days'")


def downgrade() -> None:
    op.create_table(
        "device_diagnostic_snapshots",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.UUID(),
            nullable=False,
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_device_diagnostic_snapshots_device_id_devices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_device_diagnostic_snapshots"),
    )
    op.create_index(
        "ix_device_diagnostic_snapshots_device_id_captured_at",
        "device_diagnostic_snapshots",
        ["device_id", sa.text("captured_at DESC")],
    )
