"""add device diagnostic snapshots

Persistent storage for device diagnostic bundles captured on operator
demand or automatically when a device crosses into review_required.
Cascade-deletes with the device.

Revision ID: a3e5494bb757
Revises: 3e9a8d11c7b2
Create Date: 2026-05-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "a3e5494bb757"
down_revision: str | None = "3e9a8d11c7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(
        "ix_device_diagnostic_snapshots_device_id_captured_at",
        table_name="device_diagnostic_snapshots",
    )
    op.drop_table("device_diagnostic_snapshots")
