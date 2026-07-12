"""strip retired lifecycle policy keys.

The retired ladder keys are amnesty data: downgrade is intentionally a no-op
because the append-only remediation log is the source of truth going forward.

Revision ID: 16b3cd45f86d
Revises: 225a29d636e0
Create Date: 2026-07-12 17:08:44.038285

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "16b3cd45f86d"
down_revision: str | None = "225a29d636e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE devices SET lifecycle_policy_state = lifecycle_policy_state
          - 'backoff_until' - 'recovery_backoff_attempts'
          - 'last_failure_source' - 'last_failure_reason'
          - 'last_action' - 'last_action_at'
        WHERE lifecycle_policy_state IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
