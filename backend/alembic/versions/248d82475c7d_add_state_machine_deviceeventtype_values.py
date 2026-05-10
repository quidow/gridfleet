"""add state-machine DeviceEventType values

Revision ID: 248d82475c7d
Revises: a7b4c1f9d3e8
Create Date: 2026-05-10 17:00:00.000000

Adds maintenance_entered, maintenance_exited, session_started, session_ended,
and auto_stopped to the deviceeventtype enum. Used by DeviceStateMachine's
EventLogHook to record one event row per state-changing transition.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "248d82475c7d"
down_revision: str | None = "a7b4c1f9d3e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'maintenance_entered'")
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'maintenance_exited'")
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'session_started'")
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'session_ended'")
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'auto_stopped'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; consistent with the project's
    # additive-enum migration policy (see d4f2a8c1b3e7).
    pass
