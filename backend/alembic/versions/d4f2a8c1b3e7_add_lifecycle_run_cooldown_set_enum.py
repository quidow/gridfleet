"""add lifecycle_run_cooldown_set to deviceeventtype enum

Revision ID: d4f2a8c1b3e7
Revises: c0f3e6c9a2b1
Create Date: 2026-05-03 21:00:00.000000

PR #54 added `lifecycle_run_cooldown_set` to the Python `DeviceEventType`
enum but forgot the matching `ALTER TYPE` here. Without this migration,
`release-with-cooldown` returns 500 because Postgres rejects the unknown
enum value when `record_lifecycle_incident` inserts the device event.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f2a8c1b3e7"
down_revision: Union[str, None] = "c0f3e6c9a2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'lifecycle_run_cooldown_set'")


def downgrade() -> None:
    # Postgres has no `ALTER TYPE ... DROP VALUE`. Removing an enum value safely
    # requires renaming the type, recreating it without the value, swapping the
    # column type, and dropping the old type — only worth doing if a value was
    # added by mistake. Leaving as a no-op is consistent with project policy
    # for additive enum migrations.
    pass
