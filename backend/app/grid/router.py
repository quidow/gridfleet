from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.appium_nodes.services.effective_state import compute_effective_state
from app.appium_nodes.services.node_viability import (
    device_node_accepting_new_sessions,
    device_node_is_viable,
)
from app.core.dependencies import DbDep
from app.core.timeutil import now_utc
from app.devices.dependencies import DeviceServicesDep
from app.devices.models import DeviceOperationalState
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services.allocatability import unavailable_reason
from app.devices.services.state import derive_operational_states
from app.grid.allocation import StereotypeTemplateCache, device_match_surface
from app.grid.matching import CapabilityMergeError, merge_candidates
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas import GridQueueRead, GridRouterRead, GridStatusRead
from app.hosts.models import Host
from app.lifecycle.services import remediation_log
from app.runs import service as run_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

router = APIRouter(prefix="/api/grid", tags=["grid"])
DEFAULT_RESTART_WINDOW_SEC = 120

CONTROL_PLANE_MESSAGE = "gridfleet control plane"


def _ticket_capabilities(ticket: GridSessionQueueTicket) -> dict[str, Any]:
    try:
        candidates = merge_candidates(ticket.requested_body)
    except CapabilityMergeError:
        caps = ticket.requested_body.get("capabilities")
        always = caps.get("alwaysMatch") if isinstance(caps, dict) else None
        return always if isinstance(always, dict) else {}
    return candidates[0] if candidates else {}


def _queue_entry(ticket: GridSessionQueueTicket) -> dict[str, Any]:
    """Selenium-queue-shaped (camelCase) view of one waiting ticket. Shared by the
    ``/queue`` and ``/router`` endpoints so their queue payloads cannot drift."""
    return {
        "requestId": str(ticket.id),
        "capabilities": _ticket_capabilities(ticket),
        "requestTimestamp": ticket.created_at.isoformat(),
        "runId": str(ticket.run_id) if ticket.run_id is not None else None,
    }


async def _live_sessions_by_device(db: DbDep) -> dict[Any, list[str]]:
    # running|pending via the shared chokepoint: a pending allocation
    # (create-session window) already claims its device, so the public status
    # must count it rather than report the device free (wave-5 re-review B2).
    # Deterministic order (newest first) so a device with >1 live session surfaces a
    # stable session each poll instead of flickering on Postgres row order.
    stmt = (
        select(Session.device_id, Session.session_id)
        .where(live_session_predicate())
        .order_by(Session.started_at.desc())
    )
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
    devices = await device_services.crud.list_devices_by_filters(db, DeviceQueryFilters())
    operational_states = await derive_operational_states(db, devices, now=now_utc())
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
                "operational_state": operational_states[device.id].value,
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
    return {
        "queue_size": len(waiting),
        "requests": [_queue_entry(ticket) for ticket in waiting],
    }


@router.get("/router", response_model=GridRouterRead)
async def grid_router(db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    devices = await device_services.crud.list_devices_by_filters(db, DeviceQueryFilters())
    sessions_by_device = await _live_sessions_by_device(db)
    waiting = await _waiting_tickets(db)

    hosts = (await db.execute(select(Host))).scalars().all()
    hosts_by_id = {host.id: host for host in hosts}
    # Gate-honest reservation per device, loaded once (mirrors the allocator's batch
    # load) so the per-node ``unavailable_reason`` can report ``reserved``.
    reservation_map = await run_service.get_device_reservation_map(db, [device.id for device in devices])

    template_cache: StereotypeTemplateCache = {}
    now = now_utc()
    operational_states = await derive_operational_states(db, devices, now=now)
    ladders = await remediation_log.load_ladders(db, [device.id for device in devices])

    # Seed the per-operational-state buckets from the enum itself so that adding a 6th
    # DeviceOperationalState cannot turn the `counts[...] += 1` below into a fleet-wide
    # KeyError/500 — an unmodelled state is simply dropped by response_model validation.
    counts: dict[str, int] = {state.value: 0 for state in DeviceOperationalState}
    counts.update(
        {"registered": len(devices), "running": 0, "eligible": 0, "active_sessions": 0, "queue_depth": len(waiting)}
    )

    nodes: list[dict[str, Any]] = []
    for device in devices:
        node = device.appium_node
        host = hosts_by_id.get(device.host_id)
        operational_state = operational_states[device.id]
        counts[operational_state.value] += 1

        running = bool(node and node.observed_running)
        if running:
            counts["running"] += 1

        session_ids = sessions_by_device.get(device.id, [])

        # Routability projection (design P4): node viability + warm soft-gate + gate-honest
        # reservation, the same axes the allocator's lock-time recheck applies. ``eligible``
        # mirrors ``allocation._eligible_devices`` exactly (it also excludes a device with a
        # live session, e.g. a viability probe, which ``unavailable_reason`` does not model).
        node_viable = device_node_is_viable(
            device,
            now=now,
            restart_window_sec=DEFAULT_RESTART_WINDOW_SEC,
        )
        node_accepting = device_node_accepting_new_sessions(device)
        reserved = run_service.reservation_gating_run_id(reservation_map.get(device.id), device.id) is not None
        reason = unavailable_reason(
            operational_state,
            reserved=reserved,
            accepting_new_sessions=node_accepting,
            node_viable=node_viable,
        )
        if operational_state is DeviceOperationalState.available and node_viable and node_accepting and not session_ids:
            counts["eligible"] += 1

        effective_state = None
        node_port = None
        if node is not None:
            node_port = node.port
            effective_state = compute_effective_state(
                pid=node.pid,
                desired_state=node.desired_state.value,
                health_running=node.health_running,
                health_state=node.health_state,
                restart_requested_at=node.restart_requested_at,
                started_at=node.started_at,
                restart_window_sec=DEFAULT_RESTART_WINDOW_SEC,
                lifecycle_policy_state=remediation_log.build_policy_view(
                    ladders[device.id], device.lifecycle_policy_state
                ),
                review_required=device.review_required,
                now=now,
            )

        target = None
        if running and host is not None and node_port is not None:
            target = f"http://{host.ip}:{node_port}"

        session_id = session_ids[0] if session_ids else None

        stereotype = await device_match_surface(db, device, template_cache=template_cache)

        nodes.append(
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "platform_id": device.platform_id,
                "host_id": str(device.host_id),
                "host_name": host.hostname if host is not None else None,
                "operational_state": operational_state.value,
                "node_effective_state": effective_state,
                "unavailable_reason": reason.value if reason is not None else None,
                "session_id": session_id,
                "session_target": target if session_id else None,
                "stereotype": stereotype,
            }
        )

    counts["active_sessions"] = sum(len(sids) for sids in sessions_by_device.values())

    return {
        "counts": counts,
        "nodes": nodes,
        "queue": [_queue_entry(ticket) for ticket in waiting],
    }
