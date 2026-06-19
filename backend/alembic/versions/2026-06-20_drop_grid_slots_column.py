"""drop driver_pack_platforms.grid_slots (write-dead Selenium-Grid slot advertisement)

Revision ID: 54be551fc505
Revises: 79679b99101c
Create Date: 2026-06-20 00:30:10.708066

grid_slots flowed manifest -> DB -> start payload -> agent and was never used to
build the Appium argv. No reader survives after the code removal; the stored slot
lists are unused, so the drop loses nothing operational.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54be551fc505"
down_revision: str | None = "79679b99101c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("driver_pack_platforms", "grid_slots")


def downgrade() -> None:
    op.add_column(
        "driver_pack_platforms",
        sa.Column(
            "grid_slots",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"native\"]'::jsonb"),
        ),
    )
    op.alter_column("driver_pack_platforms", "grid_slots", server_default=None)
