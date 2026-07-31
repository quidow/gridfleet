from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import asc, desc, select

from app.core.pagination import (
    CursorPage,
    CursorToken,
    decode_cursor,
    encode_cursor,
    keyset_newer,
    keyset_older,
)
from app.core.timeutil import parse_iso as _parse_datetime
from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.schemas.lifecycle import LifecycleIncidentRead
from app.devices.services.event import record_event

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher

# SSE event published for every lifecycle incident (F1). Registered in the public
# event catalog under ``device_and_node_lifecycle``.
LIFECYCLE_INCIDENT_EVENT_TYPE = "device.lifecycle_incident"

# The original 10 lifecycle_* policy incidents. Kept as its own literal (rather than
# derived by string-prefix matching on the enum value) so it can be exported as the
# policy-only scope below without drifting from LIFECYCLE_INCIDENT_LABELS.
_LIFECYCLE_POLICY_INCIDENT_LABELS: dict[DeviceEventType, str] = {
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

# Widened beyond lifecycle_* so device history shows the failure that preceded a
# recovery, not just the recovery. Session/desired-state churn stays excluded as noise.
_LIFECYCLE_FAILURE_AND_MAINTENANCE_INCIDENT_LABELS: dict[DeviceEventType, str] = {
    DeviceEventType.health_check_fail: "Health Fail",
    DeviceEventType.connectivity_lost: "Disconnected",
    DeviceEventType.connectivity_restored: "Connected",
    DeviceEventType.node_crash: "Node Crash",
    DeviceEventType.node_restart: "Node Restart",
    DeviceEventType.maintenance_entered: "Maintenance Entered",
    DeviceEventType.maintenance_exited: "Maintenance Exited",
}

LIFECYCLE_INCIDENT_LABELS: dict[DeviceEventType, str] = {
    **_LIFECYCLE_POLICY_INCIDENT_LABELS,
    **_LIFECYCLE_FAILURE_AND_MAINTENANCE_INCIDENT_LABELS,
}

LIFECYCLE_INCIDENT_TYPES: tuple[DeviceEventType, ...] = tuple(LIFECYCLE_INCIDENT_LABELS)

# Policy-only subset (the original 10 lifecycle_* types), for callers that enrich a
# small fixed-size window (e.g. AttentionCard) and would otherwise be starved by a
# single host flap writing several connectivity_lost/health_check_fail rows per device.
LIFECYCLE_POLICY_INCIDENT_TYPES: tuple[DeviceEventType, ...] = tuple(_LIFECYCLE_POLICY_INCIDENT_LABELS)

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


async def _has_incident_rows(
    db: AsyncSession,
    stmt: Select[tuple[DeviceEvent, Device]],
    predicate: ColumnElement[bool],
) -> bool:
    result = await db.execute(stmt.where(predicate).order_by(None).limit(1))
    return result.first() is not None


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
        scope: Literal["all", "policy"] = "all",
    ) -> CursorPage[LifecycleIncidentRead]:
        """Return lifecycle incidents with keyset cursor pagination.

        Cursors are opaque ``(created_at, id)`` tokens from ``app.core.pagination``,
        matching sessions and runs. The ``id`` tiebreak is what keeps events sharing a
        ``created_at`` from being skipped or duplicated across a page boundary.
        ``scope="policy"`` restricts to the original 10 lifecycle_* types, so a small
        fixed-size window (e.g. AttentionCard) can't be starved by the 7 failure/
        maintenance types a single host flap can emit several of per device.
        """
        incident_types = LIFECYCLE_POLICY_INCIDENT_TYPES if scope == "policy" else LIFECYCLE_INCIDENT_TYPES
        stmt = (
            select(DeviceEvent, Device)
            .join(Device, Device.id == DeviceEvent.device_id)
            .where(DeviceEvent.event_type.in_(incident_types))
        )
        if device_id is not None:
            stmt = stmt.where(DeviceEvent.device_id == device_id)

        page_stmt = stmt
        cursor_token = decode_cursor(cursor) if cursor else None
        if cursor_token is not None:
            predicate = (
                keyset_newer(DeviceEvent.created_at, DeviceEvent.id, cursor_token)
                if direction == "newer"
                else keyset_older(DeviceEvent.created_at, DeviceEvent.id, cursor_token)
            )
            page_stmt = page_stmt.where(predicate)

        if direction == "newer":
            page_stmt = page_stmt.order_by(asc(DeviceEvent.created_at), asc(DeviceEvent.id))
        else:
            page_stmt = page_stmt.order_by(desc(DeviceEvent.created_at), desc(DeviceEvent.id))

        result = await db.execute(page_stmt.limit(limit))
        rows = list(result.all())
        if direction == "newer":
            rows.reverse()

        if not rows:
            return CursorPage(items=[], limit=limit, next_cursor=None, prev_cursor=None)

        items = [serialize_lifecycle_incident(event, device) for event, device in rows]
        first_event = rows[0][0]
        last_event = rows[-1][0]
        has_newer = await _has_incident_rows(
            db,
            stmt,
            keyset_newer(DeviceEvent.created_at, DeviceEvent.id, CursorToken(first_event.created_at, first_event.id)),
        )
        has_older = await _has_incident_rows(
            db,
            stmt,
            keyset_older(DeviceEvent.created_at, DeviceEvent.id, CursorToken(last_event.created_at, last_event.id)),
        )
        return CursorPage(
            items=items,
            limit=limit,
            next_cursor=encode_cursor(last_event.created_at, last_event.id) if has_older else None,
            prev_cursor=encode_cursor(first_event.created_at, first_event.id) if has_newer else None,
        )
