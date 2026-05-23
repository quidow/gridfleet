"""drop auto_manage column from devices

Revision ID: 589fdc334be4
Revises: 911acfbcc715
Create Date: 2026-05-23 20:19:48.986740

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '589fdc334be4'
down_revision: Union[str, None] = '911acfbcc715'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('devices', 'auto_manage')
    op.execute("DELETE FROM settings WHERE key = 'devices.default_auto_manage'")


def downgrade() -> None:
    op.add_column('devices', sa.Column('auto_manage', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False))
