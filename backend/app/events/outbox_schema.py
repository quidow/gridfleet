from __future__ import annotations

from typing import Final

NOTIFY_CHANNEL: Final = "system_events"
CREATE_SYSTEM_EVENTS_NOTIFY_FUNCTION_SQL: Final = """
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
CREATE_SYSTEM_EVENTS_NOTIFY_TRIGGER_SQL: Final = """
CREATE TRIGGER system_events_notify_insert
AFTER INSERT ON system_events
FOR EACH ROW
EXECUTE FUNCTION notify_system_event_insert();
"""
