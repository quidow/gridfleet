"""add system event severity

Revision ID: fb5b9341a7a3
Revises: 25d460b8cdc5
Create Date: 2026-05-16 08:32:49.038021

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb5b9341a7a3"
down_revision: str | None = "25d460b8cdc5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_events",
        sa.Column("severity", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_system_events_severity",
        "system_events",
        "severity IN ('info','success','warning','critical','neutral')",
    )
    # Index name matches the project's MetaData naming convention
    # (ix → "<column_0_label>_idx"), so SQLAlchemy's mapped_column(index=True)
    # on SystemEvent.severity and this migration agree.
    op.create_index(
        "system_events_severity_idx",
        "system_events",
        ["severity"],
    )


def downgrade() -> None:
    op.drop_index("system_events_severity_idx", table_name="system_events")
    op.drop_constraint("ck_system_events_severity", "system_events", type_="check")
    op.drop_column("system_events", "severity")
