"""Internal allocation endpoints for the grid router component (spec §4).

Dark surface: nothing calls these in production until the router (Plan B)
ships. The allocate handler owns the long-poll; each attempt runs in a fresh
transaction via the services' ``session_factory`` so device row locks and
commits never pin one request-scoped session.

The spec named the doorbell pattern for queue wakeups; the 1 s re-attempt
inside the long-poll is the deliberate v1 simplification — same observable
behavior within 1 s, no cross-worker wake needed.
"""

import asyncio
import time
import uuid
from typing import cast

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Table, bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep
from app.devices.models import Device
from app.grid.allocation import (
    GRID_ALLOCATION_OUTCOME_TOTAL,
    AllocationNotPendingError,
    AllocationResult,
    AllocationService,
    node_target,
)
from app.grid.dependencies import GridServicesDep
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas_internal import (
    ActivityRequest,
    AllocateRequest,
    AllocateResponse,
    ConfirmRequest,
    EndedRequest,
    FailRequest,
    RouteEntry,
    RoutesResponse,
)
from app.grid.services_container import GridServices
from app.sessions.models import Session, SessionStatus

router = APIRouter(prefix="/internal/grid", include_in_schema=False, tags=["grid-internal"])

LONG_POLL_SEC = 25.0
RETRY_INTERVAL_SEC = 1.0


def _allocation(services: GridServices) -> AllocationService:
    if services.allocation is None:
        raise RuntimeError("grid allocation service is not wired")
    return services.allocation


async def _get_or_create_ticket(
    db: AsyncSession, payload: AllocateRequest, ticket_id: uuid.UUID | None
) -> GridSessionQueueTicket:
    if ticket_id is not None:
        existing = await db.get(GridSessionQueueTicket, ticket_id)
        if existing is not None:
            return existing
    ticket = GridSessionQueueTicket(requested_body=payload.body)
    db.add(ticket)
    await db.flush()
    return ticket


@router.post("/allocate", response_model=AllocateResponse)
async def allocate(payload: AllocateRequest, services: GridServicesDep) -> AllocateResponse | JSONResponse:
    allocation = _allocation(services)
    deadline = time.monotonic() + LONG_POLL_SEC
    ticket_id = payload.ticket
    while True:
        result: AllocationResult | None
        async with services.session_factory() as db:
            ticket = await _get_or_create_ticket(db, payload, ticket_id)
            ticket_id = ticket.id
            if ticket.status == GridQueueStatus.cancelled:
                await db.commit()
                return JSONResponse(status_code=400, content={"status": "invalid", "message": "invalid capabilities"})
            if ticket.status == GridQueueStatus.expired:
                await db.commit()
                return JSONResponse(status_code=410, content={"status": "expired", "message": "queue timeout"})
            if ticket.status == GridQueueStatus.claimed:
                # Lost-Allocated-response retry: return the original allocation rather
                # than double-claiming. A reaped claim resets the ticket to waiting and
                # falls through to a fresh attempt below.
                resumed = await allocation.resume_claimed(db, ticket=ticket)
                if resumed is not None:
                    await db.commit()
                    claim_window_sec = int(cast("int", services.settings.get("grid.claim_window_sec")))
                    return AllocateResponse(
                        status="allocated",
                        allocation_id=resumed.allocation_id,
                        target=resumed.target,
                        claim_window_sec=claim_window_sec,
                    )
            result = await allocation.try_allocate(db, ticket=ticket)
            # try_allocate cancels the ticket on an invalid body; re-read past
            # mypy's narrowing from the early returns above.
            cancelled = cast("GridQueueStatus", ticket.status) == GridQueueStatus.cancelled
            await db.commit()
        if cancelled:
            return JSONResponse(status_code=400, content={"status": "invalid", "message": "invalid capabilities"})
        if result is not None:
            claim_window_sec = int(cast("int", services.settings.get("grid.claim_window_sec")))
            return AllocateResponse(
                status="allocated",
                allocation_id=result.allocation_id,
                target=result.target,
                claim_window_sec=claim_window_sec,
            )
        if time.monotonic() >= deadline:
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="queued").inc()
            return AllocateResponse(status="queued", ticket=ticket_id)
        await asyncio.sleep(RETRY_INTERVAL_SEC)


@router.delete("/allocate/{ticket_id}", status_code=204)
async def cancel_ticket(ticket_id: uuid.UUID, db: DbDep) -> Response:
    ticket = await db.get(GridSessionQueueTicket, ticket_id)
    if ticket is not None and ticket.status == GridQueueStatus.waiting:
        ticket.status = GridQueueStatus.cancelled
        await db.commit()
    return Response(status_code=204)


@router.post("/sessions/{allocation_id}/confirm", status_code=204)
async def confirm(allocation_id: uuid.UUID, payload: ConfirmRequest, db: DbDep, services: GridServicesDep) -> Response:
    try:
        await _allocation(services).confirm(
            db, allocation_id=allocation_id, appium_session_id=payload.appium_session_id
        )
    except AllocationNotPendingError:
        return Response(status_code=409)
    await db.commit()
    return Response(status_code=204)


@router.post("/sessions/{allocation_id}/fail", status_code=204)
async def fail(allocation_id: uuid.UUID, payload: FailRequest, db: DbDep, services: GridServicesDep) -> Response:
    await _allocation(services).fail(db, allocation_id=allocation_id, message=payload.message)
    await db.commit()
    return Response(status_code=204)


@router.post("/sessions/ended", status_code=204)
async def ended(payload: EndedRequest, db: DbDep, services: GridServicesDep) -> Response:
    await _allocation(services).mark_ended(db, appium_session_id=payload.session_id)
    await db.commit()
    return Response(status_code=204)


@router.get("/routes", response_model=RoutesResponse)
async def routes(db: DbDep) -> RoutesResponse:
    stmt = (
        select(Session)
        .where(Session.status == SessionStatus.running, Session.ended_at.is_(None))
        .options(
            selectinload(Session.device).selectinload(Device.appium_node),
            selectinload(Session.device).selectinload(Device.host),
        )
    )
    rows = (await db.execute(stmt)).scalars().all()
    entries: list[RouteEntry] = []
    for row in rows:
        if row.device is None:
            continue
        target = node_target(row.device)
        if target is None:
            continue
        entries.append(RouteEntry(session_id=row.session_id, target=target))
    return RoutesResponse(routes=entries)


@router.post("/activity", status_code=204)
async def activity(payload: ActivityRequest, db: DbDep) -> Response:
    if not payload.sessions:
        return Response(status_code=204)
    # One executemany round trip instead of N serial UPDATEs: a Core UPDATE against
    # the Session table (not the ORM mapper, whose bulk path would demand PK values)
    # parameterized on the matched session_id and the new timestamp, fed the per-
    # session bind list.
    table = Session.__table__
    assert isinstance(table, Table)  # mypy narrowing; always true for a mapped model
    stmt = (
        update(table)
        .where(table.c.session_id == bindparam("b_session_id"), table.c.status == SessionStatus.running)
        .values(last_activity_at=bindparam("b_last_activity_at"))
    )
    await db.execute(
        stmt,
        [{"b_session_id": session_id, "b_last_activity_at": ts} for session_id, ts in payload.sessions.items()],
    )
    await db.commit()
    return Response(status_code=204)
