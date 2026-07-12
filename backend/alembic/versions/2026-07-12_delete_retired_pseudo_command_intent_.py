"""Delete retired pseudo-command intent rows.

The old ``health_failure:node`` row had no TTL and would otherwise produce
unknown-kind warnings on every reconcile tick after the application cutover.
Revision ID: ef5709606fa6
Revises: 16b3cd45f86d
Create Date: 2026-07-12 19:45:04.422131

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "ef5709606fa6"
down_revision: Union[str, None] = "16b3cd45f86d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM device_intents WHERE kind IN "
        "('health_failure:node', 'auto_recovery:node', 'auto_recovery:recovery')"
    )


def downgrade() -> None:
    pass
