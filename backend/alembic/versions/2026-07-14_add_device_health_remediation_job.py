"""add device health remediation job

Revision ID: d5b81a6f0c94
Revises: c7a94b1e2d63
Create Date: 2026-07-14 15:00:00.000000

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d5b81a6f0c94"
down_revision: str | None = "c7a94b1e2d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("failure_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("remediation_device_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("failure_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("remediation_action_id", sa.String(), nullable=True),
    )
    op.create_index(
        "uq_jobs_active_remediation",
        "jobs",
        ["remediation_device_id", "failure_episode_id", "remediation_action_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND remediation_device_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_remediation", table_name="jobs")
    op.drop_column("jobs", "remediation_action_id")
    op.drop_column("jobs", "failure_episode_id")
    op.drop_column("jobs", "remediation_device_id")
    op.drop_column("devices", "failure_episode_id")
