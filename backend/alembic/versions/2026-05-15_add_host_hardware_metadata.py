"""add host hardware metadata

Revision ID: 536441ef0f0a
Revises: e827486cd633
Create Date: 2026-05-15 20:40:16.568524

Adds seven nullable columns to ``hosts`` for static hardware/OS metadata
reported by the agent in the registration payload.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "536441ef0f0a"
down_revision: str | None = "e827486cd633"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("os_version", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("kernel_version", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("cpu_arch", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("cpu_model", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("cpu_cores", sa.Integer(), nullable=True))
    op.add_column("hosts", sa.Column("total_memory_mb", sa.Integer(), nullable=True))
    op.add_column("hosts", sa.Column("total_disk_gb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "total_disk_gb")
    op.drop_column("hosts", "total_memory_mb")
    op.drop_column("hosts", "cpu_cores")
    op.drop_column("hosts", "cpu_model")
    op.drop_column("hosts", "cpu_arch")
    op.drop_column("hosts", "kernel_version")
    op.drop_column("hosts", "os_version")
