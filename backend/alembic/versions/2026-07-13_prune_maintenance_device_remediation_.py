"""prune maintenance device remediation logs

Revision ID: 3bceace4dd7e
Revises: 012946a40ee2
Create Date: 2026-07-13 14:39:39.873071

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3bceace4dd7e"
down_revision: Union[str, None] = "012946a40ee2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM device_remediation_log WHERE device_id IN "
        "(SELECT id FROM devices WHERE lifecycle_policy_state->>'maintenance_reason' IS NOT NULL)"
    )


def downgrade() -> None:
    # One-time cleanup of superseded redundant rows; nothing to restore.
    pass
