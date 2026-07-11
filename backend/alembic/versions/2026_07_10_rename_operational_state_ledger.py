"""rename operational_state to the event ledger

Revision ID: 20260710_opstate_ledger
Revises: 20260710_drop_plumbing
"""

from alembic import op

revision = "20260710_opstate_ledger"
down_revision = "20260710_drop_plumbing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("devices", "operational_state", new_column_name="operational_state_last_emitted")


def downgrade() -> None:
    op.alter_column("devices", "operational_state_last_emitted", new_column_name="operational_state")
