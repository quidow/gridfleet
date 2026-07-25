"""Notify system event inserts after commit.

Revision ID: 20260722_notify_trigger
Revises: 20260721_drop_device_telemetry
Create Date: 2026-07-22

The DDL below is duplicated from ``app.events.outbox_schema`` on purpose. A
revision is a historical snapshot: importing the constants would let a later
edit to that module retroactively change what this revision does when the chain
replays from zero, and deleting or renaming the module would break
``alembic upgrade head`` on a fresh database. No migration in this tree imports
from ``app.``; four duplicated string literals are the price of replay
correctness. ``app.events.outbox_schema`` stays the single source for the
metadata ``after_create`` hook that test schemas use.

Names are deliberately unqualified so they land in the target search path
rather than ``public``.
"""

from __future__ import annotations

from alembic import op

revision = "20260722_notify_trigger"
down_revision = "20260721_drop_device_telemetry"
branch_labels = None
depends_on = None

CREATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION notify_system_event_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('system_events', NEW.id::text);
    RETURN NEW;
END;
$$;
"""
CREATE_TRIGGER_SQL = """
CREATE TRIGGER system_events_notify_insert
AFTER INSERT ON system_events
FOR EACH ROW
EXECUTE FUNCTION notify_system_event_insert();
"""
DROP_TRIGGER_SQL = "DROP TRIGGER IF EXISTS system_events_notify_insert ON system_events"
DROP_FUNCTION_SQL = "DROP FUNCTION IF EXISTS notify_system_event_insert()"


def upgrade() -> None:
    op.execute(CREATE_FUNCTION_SQL)
    op.execute(CREATE_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(DROP_TRIGGER_SQL)
    op.execute(DROP_FUNCTION_SQL)
