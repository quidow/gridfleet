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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db_retry import retry_on_serialization_failure
from app.core.dependencies import DbDep
from app.core.timeutil import now_utc
from app.devices.models import Device
from app.grid.allocation import (
    GRID_ALLOCATION_OUTCOME_TOTAL,
    AllocationNotPendingError,
    AllocationResult,
    RunNotActiveError,
    resolve_router_target,
    transition_ticket,
)
from app.grid.constants import RETRY_INTERVAL_SEC
from app.grid.dependencies import GridServicesDep
from app.grid.matching import CapabilityMergeError
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
from app.sessions.models import Session, SessionStatus

router = APIRouter(prefix="/internal/grid", include_in_schema=False, tags=["grid-internal"])

LONG_POLL_SEC = 25.0


async def _get_or_create_ticket(
    db: AsyncSession, payload: AllocateRequest, ticket_id: uuid.UUID | None
) -> GridSessionQueueTicket:
    if ticket_id is not None:
        existing = await db.get(GridSessionQueueTicket, ticket_id)
        if existing is not None:
            return existing
    ticket = GridSessionQueueTicket(requested_body=payload.body, run_id=payload.run_id)
    db.add(ticket)
    await db.flush()
    return ticket


@router.post("/allocate", response_model=AllocateResponse)
async def allocate(payload: AllocateRequest, services: GridServicesDep) -> AllocateResponse | JSONResponse:
    allocation = services.allocation
    deadline = time.monotonic() + LONG_POLL_SEC
    ticket_id = payload.ticket

    def _allocated(result: AllocationResult) -> AllocateResponse:
        return AllocateResponse(
            status="allocated",
            allocation_id=result.allocation_id,
            target=result.target,
            claim_window_sec=int(cast("int", services.settings.get("grid.claim_window_sec"))),
        )

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
                    return _allocated(resumed)
            try:
                result = await allocation.try_allocate(db, ticket=ticket)
            except (CapabilityMergeError, RunNotActiveError) as e:
                # try_allocate already cancelled the ticket; persist that and put
                # the descriptive message (merge error or inactive run) in the 400
                # body (wave-5 #26) — the router maps it to a W3C
                # session-not-created. A re-poll of the cancelled ticket gets the
                # generic text above.
                await db.commit()
                return JSONResponse(status_code=400, content={"status": "invalid", "message": str(e)})
            await db.commit()
        if result is not None:
            return _allocated(result)
        if time.monotonic() >= deadline:
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="queued").inc()
            return AllocateResponse(status="queued", ticket=ticket_id)
        await asyncio.sleep(RETRY_INTERVAL_SEC)


@router.delete("/allocate/{ticket_id}", status_code=204)
async def cancel_ticket(ticket_id: uuid.UUID, db: DbDep) -> Response:
    ticket = await db.get(GridSessionQueueTicket, ticket_id)
    if ticket is not None and ticket.status == GridQueueStatus.waiting:
        transition_ticket(ticket, GridQueueStatus.cancelled, reason="router_cancelled")
        await db.commit()
    return Response(status_code=204)


@router.post("/sessions/{allocation_id}/confirm", status_code=204)
async def confirm(allocation_id: uuid.UUID, payload: ConfirmRequest, db: DbDep, services: GridServicesDep) -> Response:
    try:
        await services.allocation.confirm(
            db,
            allocation_id=allocation_id,
            appium_session_id=payload.appium_session_id,
            appium_capabilities=payload.appium_capabilities,
        )
    except AllocationNotPendingError:
        # The router rolls the just-created Appium session back best-effort on 409.
        # Record the reported id on the terminal row first (wave-5 #7) so the orphan
        # sweep can kill exactly this session if that rollback DELETE fails — even
        # while the device already holds a new pending allocation.
        await services.allocation.record_doomed_appium_session(
            db, allocation_id=allocation_id, appium_session_id=payload.appium_session_id
        )
        await db.commit()
        return Response(status_code=409)
    await db.commit()
    return Response(status_code=204)


@router.post("/sessions/{allocation_id}/fail", status_code=204)
async def fail(allocation_id: uuid.UUID, payload: FailRequest, db: DbDep, services: GridServicesDep) -> Response:
    await services.allocation.fail(db, allocation_id=allocation_id, message=payload.message)
    await db.commit()
    return Response(status_code=204)


@router.post("/sessions/ended", status_code=204)
async def ended(payload: EndedRequest, db: DbDep, services: GridServicesDep) -> Response:
    async def _attempt() -> None:
        await services.allocation.mark_ended(db, appium_session_id=payload.session_id)
        await db.commit()

    await retry_on_serialization_failure(db, _attempt, caller="grid_session_ended")
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
        # Prefer the live node target; fall back to the target stored at allocation so a
        # running session whose device's node port was transiently stale-cleared
        # (recovery backoff) does not vanish from the route table mid-flight (#6). Only
        # skip when both are null.
        target = resolve_router_target(row)
        if target is None:
            continue
        entries.append(RouteEntry(session_id=row.session_id, target=target))
    return RoutesResponse(routes=entries)


@router.post("/activity", status_code=204)
async def activity(payload: ActivityRequest, db: DbDep) -> Response:
    if not payload.sessions:
        return Response(status_code=204)
    # The payload means "these sessions were active"; stamp a single server-side
    # now() for every reported session (the legacy map form's caller datetimes were
    # always ignored — router clock skew would otherwise extend or defeat idle
    # reaping, which is judged against this host's clock). One UPDATE over an IN-set.
    now = now_utc()
    await db.execute(
        update(Session)
        .where(Session.session_id.in_(payload.session_ids), Session.status == SessionStatus.running)
        .values(last_activity_at=now)
    )
    await db.commit()
    return Response(status_code=204)
