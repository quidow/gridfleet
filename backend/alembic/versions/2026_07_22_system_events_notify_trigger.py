"""Notify system event inserts after commit.

Revision ID: 20260722_notify_trigger
Revises: 20260721_drop_device_telemetry
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op
from app.events.outbox_schema import (
    CREATE_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL,
    CREATE_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL,
    DROP_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL,
    DROP_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL,
)

revision = "20260722_notify_trigger"
down_revision = "20260721_drop_device_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CREATE_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL)
    op.execute(CREATE_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(DROP_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL)
    op.execute(DROP_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL)
