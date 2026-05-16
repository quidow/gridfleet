import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.events import (
    EVENT_CATEGORY_DISPLAY_NAMES,
    PUBLIC_EVENT_CATALOG,
    Event,
    event_bus,
    validate_public_event_names,
)
from app.events.catalog import ALL_SEVERITIES
from app.events.schemas import NotificationListRead
from app.events.schemas_catalog import EventCatalogRead

router = APIRouter(prefix="/api", tags=["events"])

KEEPALIVE_INTERVAL = 15


async def _wait_for_queue_event(queue: asyncio.Queue[Event], *, timeout: float | None = None) -> Event:
    get_task = asyncio.create_task(queue.get())
    try:
        if timeout is None:
            return await get_task
        return await asyncio.wait_for(get_task, timeout=timeout)
    finally:
        if not get_task.done():
            get_task.cancel()
            _ = await asyncio.gather(get_task, return_exceptions=True)


@router.get("/events/catalog", response_model=EventCatalogRead)
async def get_event_catalog() -> dict[str, Any]:
    return {
        "events": [
            {
                "name": event.name,
                "category": event.category,
                "category_display_name": EVENT_CATEGORY_DISPLAY_NAMES[event.category],
                "description": event.description,
                "default_severity": event.default_severity,
                "allowed_severities": sorted(event.allowed_severities),
                "typical_data_fields": list(event.typical_data_fields),
            }
            for event in PUBLIC_EVENT_CATALOG
        ]
    }


@router.get("/events")
async def event_stream(
    request: Request,
    types: str | None = Query(None, description="Comma-separated event types to filter"),
    device_ids: str | None = Query(None, description="Comma-separated device UUIDs to filter"),
) -> EventSourceResponse:
    type_filter = {t.strip() for t in types.split(",")} if types else None
    device_filter = {d.strip() for d in device_ids.split(",")} if device_ids else None

    queue = event_bus.subscribe()

    async def generate() -> AsyncGenerator[dict[str, str], None]:
        try:
            while True:
                try:
                    event = await _wait_for_queue_event(queue, timeout=KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield {"comment": "keepalive"}
                    continue

                if type_filter and event.type not in type_filter:
                    continue
                if device_filter:
                    event_device_id = event.data.get("device_id")
                    if event_device_id and str(event_device_id) not in device_filter:
                        continue

                yield {
                    "event": event.type,
                    "id": event.id,
                    "data": json.dumps(event.to_dict()),
                }
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(generate(), ping=KEEPALIVE_INTERVAL)


def _parse_severity_filter(raw: str) -> list[str] | None:
    values = [token.strip() for token in raw.split(",") if token.strip()]
    if not values:
        return None
    invalid = sorted({v for v in values if v not in ALL_SEVERITIES})
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown severity value(s): {', '.join(invalid)}",
        )
    return values


def _parse_types_filter(raw: str) -> list[str] | None:
    values = [token.strip() for token in raw.split(",") if token.strip()]
    if not values:
        return None
    try:
        return validate_public_event_names(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/notifications", response_model=NotificationListRead)
async def get_notifications(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    types: str | None = Query(None, description="Comma-separated event types to filter"),
    severity: str | None = Query(None, description="Comma-separated severities to filter"),
) -> dict[str, Any]:
    type_filter = _parse_types_filter(types) if types is not None else None
    severity_filter = _parse_severity_filter(severity) if severity is not None else None
    events, total = await event_bus.get_recent_events_persisted(
        limit=limit,
        offset=offset,
        event_types=type_filter,
        severities=severity_filter,
    )
    return {
        "items": events,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
