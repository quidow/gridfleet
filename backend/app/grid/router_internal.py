"""Internal grid endpoints for the grid router component.

The Rust router on :4444 calls these to create/end Appium sessions, cancel
tickets, and fetch the live route table. Mounted under
``/internal/grid`` and auth-gated in ``app.main``. The create-session handler owns
the long-poll; each attempt runs in a fresh transaction via the services'
``session_factory`` so device row locks and commits never pin one
request-scoped session.

The 1 s re-attempt inside the long-poll is the deliberate v1 simplification
for queue wakeups — same observable behavior within 1 s, no cross-worker wake
needed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.db_retry import retry_on_serialization_failure
from app.core.dependencies import DbDep
from app.core.timeutil import now_utc
from app.devices.models import Device
from app.grid import session_create
from app.grid.allocation import (
    GRID_ALLOCATE_QUEUE_WAIT_SECONDS,
    GRID_ALLOCATION_OUTCOME_TOTAL,
    GRID_TRY_ALLOCATE_DURATION_SECONDS,
    AllocationResult,
    RunNotActiveError,
    resolve_router_target,
    transition_ticket,
)
from app.grid.constants import LONG_POLL_SEC, RETRY_INTERVAL_SEC
from app.grid.dependencies import GridServicesDep
from app.grid.matching import CapabilityMergeError
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas_internal import (
    ActivityRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    EndedRequest,
    RouteEntry,
    RoutesResponse,
)
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/internal/grid", include_in_schema=False, tags=["grid-internal"])


def _wire_status_for_create_outcome(
    outcome: session_create.CreateOutcome,
) -> Literal["created", "create_failed", "create_error"]:
    if outcome.kind == "created":
        return "created"
    if outcome.kind == "w3c_rejected":
        return "create_failed"
    return "create_error"


async def _get_or_create_ticket(
    db: AsyncSession, payload: CreateSessionRequest, ticket_id: uuid.UUID | None
) -> GridSessionQueueTicket:
    if ticket_id is not None:
        existing = await db.get(GridSessionQueueTicket, ticket_id)
        if existing is not None:
            return existing
    ticket = GridSessionQueueTicket(requested_body=payload.body, run_id=payload.run_id)
    db.add(ticket)
    await db.flush()
    return ticket


@router.post("/create-session", response_model=CreateSessionResponse)
async def create_session(
    payload: CreateSessionRequest, services: GridServicesDep
) -> CreateSessionResponse | JSONResponse:
    allocation = services.allocation
    raw_body = json.dumps(payload.body, separators=(",", ":")).encode("utf-8")
    started = time.monotonic()
    deadline = started + LONG_POLL_SEC
    ticket_id = payload.ticket

    if ticket_id is not None:
        async with services.session_factory() as db:
            await allocation.resume_interrupted(db, ticket_id=ticket_id)
            await db.commit()

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
            attempt_started = time.monotonic()
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
            GRID_TRY_ALLOCATE_DURATION_SECONDS.observe(time.monotonic() - attempt_started)
            await db.commit()
        if result is not None:
            GRID_ALLOCATE_QUEUE_WAIT_SECONDS.labels(outcome="allocated").observe(time.monotonic() - started)
            claim_window = int(cast("int", services.settings.get("grid.claim_window_sec")))
            outcome = await session_create.create_and_promote(
                services.session_factory,
                allocation,
                allocation=result,
                raw_body=raw_body,
                claim_window_sec=claim_window,
            )
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome=outcome.kind).inc()
            return CreateSessionResponse(
                status=_wire_status_for_create_outcome(outcome),
                session_id=outcome.session_id or None,
                target=result.target if outcome.kind == "created" else None,
                device_id=result.device_id if outcome.kind == "created" else None,
                appium_status=outcome.appium_status or None,
                appium_body=outcome.appium_body,
                message=outcome.message or None,
            )
        if time.monotonic() >= deadline:
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="queued").inc()
            GRID_ALLOCATE_QUEUE_WAIT_SECONDS.labels(outcome="queued").observe(time.monotonic() - started)
            return CreateSessionResponse(status="queued", ticket=ticket_id)
        await asyncio.sleep(RETRY_INTERVAL_SEC)


@router.delete("/tickets/{ticket_id}", status_code=204)
async def cancel_ticket(ticket_id: uuid.UUID, db: DbDep) -> Response:
    ticket = await db.get(GridSessionQueueTicket, ticket_id)
    if ticket is not None and ticket.status == GridQueueStatus.waiting:
        transition_ticket(ticket, GridQueueStatus.cancelled, reason="router_cancelled")
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
        .where(Session.session_id.in_(payload.sessions), Session.status == SessionStatus.running)
        .values(last_activity_at=now)
    )
    await db.commit()
    return Response(status_code=204)
