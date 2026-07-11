"""drop host_sweep observation fold state

Revision ID: aeea5f241bcb
Revises: 2026_07_11_drop_presentation
Create Date: 2026-07-11 22:27:21.327613

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'aeea5f241bcb'
down_revision: Union[str, None] = '2026_07_11_drop_presentation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM control_plane_state_entries WHERE namespace = 'host_sweep.observation_fold'")


def downgrade() -> None:
    pass
