from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.core.dependencies import DbDep
from app.devices.dependencies import DeviceServicesDep
from app.grid.matching import CapabilityMergeError, merge_candidates
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas import GridQueueRead, GridStatusRead
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

router = APIRouter(prefix="/api/grid", tags=["grid"])

CONTROL_PLANE_MESSAGE = "gridfleet control plane"


def _ticket_capabilities(ticket: GridSessionQueueTicket) -> dict[str, Any]:
    try:
        candidates = merge_candidates(ticket.requested_body)
    except CapabilityMergeError:
        caps = ticket.requested_body.get("capabilities")
        always = caps.get("alwaysMatch") if isinstance(caps, dict) else None
        return always if isinstance(always, dict) else {}
    return candidates[0] if candidates else {}


async def _live_sessions_by_device(db: DbDep) -> dict[Any, list[str]]:
    # running|pending via the shared chokepoint: a pending allocation
    # (allocate->confirm window) already claims its device, so the public status
    # must count it rather than report the device free (wave-5 re-review B2).
    stmt = select(Session.device_id, Session.session_id).where(live_session_predicate())
    rows = (await db.execute(stmt)).all()
    by_device: dict[Any, list[str]] = {}
    for device_id, session_id in rows:
        if device_id is None:
            continue
        by_device.setdefault(device_id, []).append(session_id)
    return by_device


async def _waiting_tickets(db: DbDep) -> list[GridSessionQueueTicket]:
    stmt = (
        select(GridSessionQueueTicket)
        .where(GridSessionQueueTicket.status == GridQueueStatus.waiting)
        .order_by(GridSessionQueueTicket.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


@router.get("/status", response_model=GridStatusRead)
async def grid_status(db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    devices = await device_services.crud.list_devices(db)
    sessions_by_device = await _live_sessions_by_device(db)
    waiting = await _waiting_tickets(db)

    registry_devices = []
    for device in devices:
        node = device.appium_node
        running = bool(node and node.observed_running)
        registry_devices.append(
            {
                "id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "name": device.name,
                "platform_id": device.platform_id,
                "operational_state": device.operational_state.value,
                "node_state": ("running" if running else "stopped") if node else None,
                "node_port": node.port if node else None,
            }
        )

    active_session_ids = [sid for sids in sessions_by_device.values() for sid in sids]
    running_node_count = sum(1 for device in devices if device.appium_node and device.appium_node.observed_running)
    return {
        "ready": True,
        "message": CONTROL_PLANE_MESSAGE,
        "registry": {"device_count": len(registry_devices), "devices": registry_devices},
        "active_sessions": len(active_session_ids),
        "active_session_ids": active_session_ids,
        "running_node_count": running_node_count,
        "queue_size": len(waiting),
        "queued_request_ids": [str(ticket.id) for ticket in waiting],
    }


@router.get("/queue", response_model=GridQueueRead)
async def grid_queue(db: DbDep) -> dict[str, Any]:
    waiting = await _waiting_tickets(db)
    requests = [
        {
            "requestId": str(ticket.id),
            "capabilities": _ticket_capabilities(ticket),
            "requestTimestamp": ticket.created_at.isoformat(),
            "runId": str(ticket.run_id) if ticket.run_id is not None else None,
        }
        for ticket in waiting
    ]
    return {
        "queue_size": len(waiting),
        "requests": requests,
    }
