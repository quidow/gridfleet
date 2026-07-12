"""Strip deferred-stop keys from lifecycle policy state.

Deferrals armed since WS-15.1 already have remediation-log action rows, so
derivation carries live episodes across this one-way cleanup.

Revision ID: 012946a40ee2
Revises: ef5709606fa6
Create Date: 2026-07-12 20:09:46.889441

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '012946a40ee2'
down_revision: Union[str, None] = 'ef5709606fa6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE devices SET lifecycle_policy_state = lifecycle_policy_state
          - 'deferred_stop' - 'deferred_stop_reason' - 'deferred_stop_since'
        WHERE lifecycle_policy_state IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
