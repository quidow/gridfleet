from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.timeutil import parse_iso as _parse_datetime
from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.schemas.lifecycle import LifecycleIncidentRead
from app.devices.services.event import record_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher

# SSE event published for every lifecycle incident (F1). Registered in the public
# event catalog under ``device_and_node_lifecycle``.
LIFECYCLE_INCIDENT_EVENT_TYPE = "device.lifecycle_incident"

LIFECYCLE_INCIDENT_LABELS: dict[DeviceEventType, str] = {
    DeviceEventType.lifecycle_deferred_stop: "Stopping Soon",
    DeviceEventType.lifecycle_auto_stopped: "Auto-Stopped",
    DeviceEventType.lifecycle_recovery_suppressed: "Recovery Paused",
    DeviceEventType.lifecycle_recovery_failed: "Recovery Failed",
    DeviceEventType.lifecycle_recovery_backoff: "Waiting to Retry",
    DeviceEventType.lifecycle_recovered: "Recovered",
    DeviceEventType.lifecycle_run_excluded: "Removed from Run",
    DeviceEventType.lifecycle_run_restored: "Rejoined Run",
    DeviceEventType.lifecycle_run_cooldown_set: "Run Cooldown",
    DeviceEventType.lifecycle_run_cooldown_escalated: "Cooldown Extended",
}

LIFECYCLE_INCIDENT_TYPES: tuple[DeviceEventType, ...] = tuple(LIFECYCLE_INCIDENT_LABELS)

# SSE severity per incident type. A recovered/rejoined device is good news (success);
# a failed recovery is operator-actionable (critical); auto-stop / paused / extended
# cooldown warrant a warning; the rest are informational. Default: info.
_LIFECYCLE_INCIDENT_SEVERITY: dict[DeviceEventType, EventSeverity] = {
    DeviceEventType.lifecycle_recovered: "success",
    DeviceEventType.lifecycle_run_restored: "success",
    DeviceEventType.lifecycle_recovery_failed: "critical",
    DeviceEventType.lifecycle_auto_stopped: "warning",
    DeviceEventType.lifecycle_recovery_suppressed: "warning",
    DeviceEventType.lifecycle_run_cooldown_escalated: "warning",
}


def _parse_summary_state(raw: object) -> DeviceLifecyclePolicySummaryState:
    if isinstance(raw, DeviceLifecyclePolicySummaryState):
        return raw
    if isinstance(raw, str):
        try:
            return DeviceLifecyclePolicySummaryState(raw)
        except ValueError:
            pass
    return DeviceLifecyclePolicySummaryState.idle


def serialize_lifecycle_incident(event: DeviceEvent, device: Device) -> LifecycleIncidentRead:
    details = event.details if isinstance(event.details, dict) else {}
    raw_run_id = details.get("run_id")
    run_id: uuid.UUID | None = None
    if isinstance(raw_run_id, str):
        try:
            run_id = uuid.UUID(raw_run_id)
        except ValueError:
            run_id = None

    return LifecycleIncidentRead(
        id=event.id,
        device_id=device.id,
        device_name=device.name,
        device_identity_value=device.identity_value,
        platform_id=device.platform_id,
        event_type=event.event_type,
        label=LIFECYCLE_INCIDENT_LABELS[event.event_type],
        summary_state=_parse_summary_state(details.get("summary_state")),
        reason=details.get("reason") if isinstance(details.get("reason"), str) else None,
        detail=details.get("detail") if isinstance(details.get("detail"), str) else None,
        source=details.get("source") if isinstance(details.get("source"), str) else None,
        run_id=run_id,
        run_name=details.get("run_name") if isinstance(details.get("run_name"), str) else None,
        backoff_until=_parse_datetime(details.get("backoff_until")),
        created_at=event.created_at,
    )


@dataclass(frozen=True, slots=True)
class LifecycleIncidentDetails:
    """Cohesive descriptive payload for a recorded lifecycle incident.

    Groups the incident-metadata fields formerly passed as individual keyword
    arguments to ``record_lifecycle_incident``. The structural arguments (db
    session, device, event type) stay direct parameters of the method.
    """

    summary_state: DeviceLifecyclePolicySummaryState
    reason: str | None = None
    detail: str | None = None
    source: str | None = None
    run_id: uuid.UUID | str | None = None
    run_name: str | None = None
    backoff_until: str | datetime | None = None


class LifecycleIncidentService:
    """Container-held facade for the device lifecycle-incident surface."""

    def __init__(self, publisher: EventPublisher | None = None) -> None:
        # Optional so the ~88 no-arg test construction sites keep working; production
        # (composition.py) injects the event bus so incidents reach SSE (F1).
        self._publisher = publisher

    async def record_lifecycle_incident(
        self,
        db: AsyncSession,
        device: Device,
        event_type: DeviceEventType,
        incident: LifecycleIncidentDetails,
    ) -> DeviceEvent:
        details: dict[str, Any] = {"summary_state": incident.summary_state.value}
        if incident.reason is not None:
            details["reason"] = incident.reason
        if incident.detail is not None:
            details["detail"] = incident.detail
        if incident.source is not None:
            details["source"] = incident.source
        if incident.run_id is not None:
            details["run_id"] = str(incident.run_id)
        if incident.run_name is not None:
            details["run_name"] = incident.run_name
        if isinstance(incident.backoff_until, datetime):
            details["backoff_until"] = incident.backoff_until.isoformat()
        elif incident.backoff_until is not None:
            details["backoff_until"] = incident.backoff_until

        event = await record_event(db, device.id, event_type, details)

        # F1: also publish to the event bus so operators get a live SSE signal of recovery
        # failing/backing off, not just a row in the device_events audit table. Queued to
        # dispatch after the caller's transaction commits (dropped on rollback).
        if self._publisher is not None:
            self._publisher.queue_for_session(
                db,
                LIFECYCLE_INCIDENT_EVENT_TYPE,
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "event_type": event_type.value,
                    "label": LIFECYCLE_INCIDENT_LABELS.get(event_type),
                    "summary_state": incident.summary_state.value,
                    "reason": incident.reason,
                    "detail": incident.detail,
                    "source": incident.source,
                    "run_id": str(incident.run_id) if incident.run_id is not None else None,
                    "run_name": incident.run_name,
                },
                severity=_LIFECYCLE_INCIDENT_SEVERITY.get(event_type, "info"),
            )

        return event

    async def list_lifecycle_incidents_paginated(
        self,
        db: AsyncSession,
        *,
        limit: int = 50,
        device_id: uuid.UUID | None = None,
        cursor: str | None = None,
        direction: str = "older",
    ) -> tuple[list[LifecycleIncidentRead], str | None, str | None]:
        """Return lifecycle incidents with cursor-based pagination.

        Returns (items, next_cursor, prev_cursor).
        Cursor is the ISO timestamp of the boundary event's created_at.
        """
        stmt = (
            select(DeviceEvent, Device)
            .join(Device, Device.id == DeviceEvent.device_id)
            .where(DeviceEvent.event_type.in_(LIFECYCLE_INCIDENT_TYPES))
        )
        if device_id is not None:
            stmt = stmt.where(DeviceEvent.device_id == device_id)

        if cursor:
            cursor_dt = _parse_datetime(cursor)
            if cursor_dt is not None:
                if direction == "newer":
                    stmt = stmt.where(DeviceEvent.created_at > cursor_dt).order_by(DeviceEvent.created_at.asc())
                else:
                    stmt = stmt.where(DeviceEvent.created_at < cursor_dt).order_by(DeviceEvent.created_at.desc())
            else:
                stmt = stmt.order_by(DeviceEvent.created_at.desc())
        else:
            stmt = stmt.order_by(DeviceEvent.created_at.desc())

        stmt = stmt.limit(limit + 1)
        result = await db.execute(stmt)
        rows = result.all()

        if direction == "newer" and cursor:
            rows = list(reversed(rows))

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        items = [serialize_lifecycle_incident(event, device) for event, device in rows]

        next_cursor: str | None = None
        prev_cursor: str | None = None
        if items:
            next_cursor = items[-1].created_at.isoformat() if has_more else None
            prev_cursor = items[0].created_at.isoformat() if cursor else None

        return items, next_cursor, prev_cursor
