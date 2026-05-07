from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.device import Device
from app.models.device_event import DeviceEvent, DeviceEventType
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.schemas.lifecycle import LifecycleIncidentRead
from app.services.device_event_service import record_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

LIFECYCLE_INCIDENT_LABELS: dict[DeviceEventType, str] = {
    DeviceEventType.lifecycle_deferred_stop: "Deferred Stop",
    DeviceEventType.lifecycle_auto_stopped: "Automatic Stop",
    DeviceEventType.lifecycle_recovery_suppressed: "Recovery Suppressed",
    DeviceEventType.lifecycle_recovery_failed: "Recovery Failed",
    DeviceEventType.lifecycle_recovery_backoff: "Recovery Backoff",
    DeviceEventType.lifecycle_recovered: "Recovered",
    DeviceEventType.lifecycle_run_excluded: "Run Excluded",
    DeviceEventType.lifecycle_run_restored: "Run Restored",
    DeviceEventType.lifecycle_run_cooldown_set: "Run Cooldown",
    DeviceEventType.lifecycle_run_cooldown_escalated: "Cooldown Escalated",
}

LIFECYCLE_INCIDENT_TYPES: tuple[DeviceEventType, ...] = tuple(LIFECYCLE_INCIDENT_LABELS)


async def record_lifecycle_incident(
    db: AsyncSession,
    device: Device,
    event_type: DeviceEventType,
    *,
    summary_state: DeviceLifecyclePolicySummaryState,
    reason: str | None = None,
    detail: str | None = None,
    source: str | None = None,
    run_id: uuid.UUID | str | None = None,
    run_name: str | None = None,
    backoff_until: str | datetime | None = None,
    ttl_seconds: int | None = None,
    worker_id: str | None = None,
    expires_at: str | datetime | None = None,
) -> DeviceEvent:
    details: dict[str, Any] = {"summary_state": summary_state.value}
    if reason is not None:
        details["reason"] = reason
    if detail is not None:
        details["detail"] = detail
    if source is not None:
        details["source"] = source
    if run_id is not None:
        details["run_id"] = str(run_id)
    if run_name is not None:
        details["run_name"] = run_name
    if isinstance(backoff_until, datetime):
        details["backoff_until"] = backoff_until.isoformat()
    elif backoff_until is not None:
        details["backoff_until"] = backoff_until
    if ttl_seconds is not None:
        details["ttl_seconds"] = ttl_seconds
    if worker_id is not None:
        details["worker_id"] = worker_id
    if isinstance(expires_at, datetime):
        details["expires_at"] = expires_at.isoformat()
    elif expires_at is not None:
        details["expires_at"] = expires_at

    return await record_event(db, device.id, event_type, details)


def _parse_summary_state(raw: object) -> DeviceLifecyclePolicySummaryState:
    if isinstance(raw, DeviceLifecyclePolicySummaryState):
        return raw
    if isinstance(raw, str):
        try:
            return DeviceLifecyclePolicySummaryState(raw)
        except ValueError:
            pass
    return DeviceLifecyclePolicySummaryState.idle


def _parse_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


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


async def list_lifecycle_incidents_paginated(
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


async def list_lifecycle_incidents(
    db: AsyncSession,
    *,
    limit: int = 50,
    device_id: uuid.UUID | None = None,
) -> list[LifecycleIncidentRead]:
    stmt = (
        select(DeviceEvent, Device)
        .join(Device, Device.id == DeviceEvent.device_id)
        .where(DeviceEvent.event_type.in_(LIFECYCLE_INCIDENT_TYPES))
        .order_by(DeviceEvent.created_at.desc())
        .limit(limit)
    )
    if device_id is not None:
        stmt = stmt.where(DeviceEvent.device_id == device_id)

    result = await db.execute(stmt)
    return [serialize_lifecycle_incident(event, device) for event, device in result.all()]
