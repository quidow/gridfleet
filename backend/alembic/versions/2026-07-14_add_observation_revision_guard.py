"""add two-axis observation-revision guard columns and sequence

Revision ID: a1f3c9d27b60
Revises: 74233793ee45
Create Date: 2026-07-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c9d27b60'
down_revision: Union[str, None] = '74233793ee45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEQUENCE_NAME = "observation_revision_seq"


def upgrade() -> None:
    # A single backend-issued monotonic revision source for the two-axis health
    # write-ordering guard: strictly monotonic and non-transactional (nextval
    # never rolls back), shared across every API worker and the scheduler.
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME}"))
    op.add_column(
        "appium_nodes",
        sa.Column(
            "health_observation_revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "device_checks_observation_revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("devices", "device_checks_observation_revision")
    op.drop_column("appium_nodes", "health_observation_revision")
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}"))
