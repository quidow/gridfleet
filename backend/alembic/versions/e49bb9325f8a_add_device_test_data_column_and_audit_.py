"""add device test_data column and audit log

Revision ID: e49bb9325f8a
Revises: b9d4f7e2a1c6
Create Date: 2026-05-08 11:20:01.682939

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e49bb9325f8a"
down_revision = "b9d4f7e2a1c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "test_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "device_test_data_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("previous_test_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_test_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_by", sa.String(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_device_test_data_audit_logs_changed_at",
        "device_test_data_audit_logs",
        ["changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_test_data_audit_logs_changed_at",
        table_name="device_test_data_audit_logs",
    )
    op.drop_table("device_test_data_audit_logs")
    op.drop_column("devices", "test_data")
