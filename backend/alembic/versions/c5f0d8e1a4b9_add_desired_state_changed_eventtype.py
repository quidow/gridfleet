"""add desired_state_changed DeviceEventType value

Revision ID: c5f0d8e1a4b9
Revises: b4f9c2e7a3d8
Create Date: 2026-05-10 19:00:00.000000

Adds desired_state_changed to the deviceeventtype enum. Used by Phase 3
desired-state writers to record one event per intent mutation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c5f0d8e1a4b9"
down_revision: str | None = "b4f9c2e7a3d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE deviceeventtype ADD VALUE IF NOT EXISTS 'desired_state_changed'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values without rebuilding the enum. Existing
    # additive enum migrations in this project use no-op downgrades.
    pass
