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
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.db_retry import retry_on_serialization_failure
from app.core.dependencies import DbDep
from app.core.timeutil import now_utc
from app.devices.models import Device
from app.grid import appium_direct, session_create
from app.grid.allocation import (
    GRID_ALLOCATE_QUEUE_WAIT_SECONDS,
    GRID_ALLOCATION_OUTCOME_TOTAL,
    GRID_TRY_ALLOCATE_DURATION_SECONDS,
    AllocationResult,
    AllocationService,
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
from app.grid.session_create import CREATE_TIMEOUT_CAP_SEC, CREATE_TIMEOUT_MARGIN_SEC
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/internal/grid", include_in_schema=False, tags=["grid-internal"])

MAX_TARGET_ATTEMPTS = 3
PER_ATTEMPT_MIN_BUDGET_SEC = 20.0
DEFAULT_CREATE_BUDGET_SEC = float(CREATE_TIMEOUT_CAP_SEC + int(LONG_POLL_SEC))


def _response_for_outcome(
    outcome: session_create.CreateOutcome,
    result: AllocationResult,
) -> CreateSessionResponse:
    if outcome.kind == "created":
        return CreateSessionResponse(
            status="created",
            session_id=outcome.session_id or None,
            target=result.target,
            device_id=result.device_id,
            appium_status=outcome.appium_status or None,
            appium_body=outcome.appium_body,
            message=outcome.message or None,
        )
    if outcome.kind == "w3c_rejected":
        return CreateSessionResponse(
            status="create_failed",
            appium_status=outcome.appium_status or None,
            appium_body=outcome.appium_body,
            message=outcome.message or None,
        )
    return CreateSessionResponse(status="create_error", message=outcome.message or None)


async def lock_ticket(db: AsyncSession, ticket_id: uuid.UUID) -> GridSessionQueueTicket | None:
    """Optional owning-root lock: ``SELECT ... FOR UPDATE`` on the queue ticket.

    Acquired before ``try_allocate`` so two concurrent polls carrying the same
    ticket id serialize on the root rather than each claiming a different device.
    Returns ``None`` when the ticket was deleted by a prior claim — the caller
    then mints a fresh ticket.
    """
    return (
        await db.execute(
            select(GridSessionQueueTicket)
            .where(GridSessionQueueTicket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _get_or_create_ticket(
    db: AsyncSession, payload: CreateSessionRequest, ticket_id: uuid.UUID | None
) -> GridSessionQueueTicket:
    if ticket_id is not None:
        existing = await lock_ticket(db, ticket_id)
        if existing is not None:
            return existing
    ticket = GridSessionQueueTicket(requested_body=payload.body, run_id=payload.run_id)
    db.add(ticket)
    await db.flush()
    return ticket


async def _finalize_interrupted_create(
    services: GridServicesDep, allocation: AllocationService, ticket_id: uuid.UUID
) -> None:
    """Prepare/effect/finalize an interrupted create.

    Resolve the live row and its immutable routing values in one transaction,
    terminate the remote Appium session with no DB context, then close the row in
    a fresh transaction via the retry helper.
    """
    async with services.session_factory.begin() as db:
        interrupted = await allocation.prepare_interrupted_session(db, ticket_id=ticket_id)
    if interrupted is not None:
        await appium_direct.terminate_session(interrupted.target, interrupted.appium_session_id)
        await retry_on_serialization_failure(
            services.session_factory,
            lambda db: allocation.mark_ended(db, appium_session_id=interrupted.appium_session_id),
            caller="grid_resume_interrupted",
        )


@router.post("/create-session", response_model=CreateSessionResponse)
async def create_session(
    payload: CreateSessionRequest,
    services: GridServicesDep,
    create_budget_ms: Annotated[
        int | None,
        Header(alias="X-Gridfleet-Create-Budget-Ms"),
    ] = None,
) -> CreateSessionResponse | JSONResponse:
    allocation = services.allocation
    raw_body = json.dumps(payload.body, separators=(",", ":")).encode("utf-8")
    started = time.monotonic()
    create_deadline = started + (
        create_budget_ms / 1000.0 if create_budget_ms is not None else DEFAULT_CREATE_BUDGET_SEC
    )
    poll_deadline = min(started + LONG_POLL_SEC, create_deadline)
    ticket_id = payload.ticket
    excluded: set[uuid.UUID] = set()
    attempts = 0
    last_target_failure_message: str | None = None

    if ticket_id is not None:
        await _finalize_interrupted_create(services, allocation, ticket_id)

    while True:
        if attempts > 0 and create_deadline - time.monotonic() < PER_ATTEMPT_MIN_BUDGET_SEC:
            return CreateSessionResponse(status="create_error", message=last_target_failure_message)
        result: AllocationResult | None
        terminal_response: JSONResponse | None = None
        async with services.session_factory.begin() as db:
            ticket = await _get_or_create_ticket(db, payload, ticket_id)
            ticket_id = ticket.id
            if ticket.status in {GridQueueStatus.cancelled, GridQueueStatus.expired}:
                is_cancelled = ticket.status == GridQueueStatus.cancelled
                terminal_response = JSONResponse(
                    status_code=400 if is_cancelled else 410,
                    content={
                        "status": "invalid" if is_cancelled else "expired",
                        "message": "invalid capabilities" if is_cancelled else "queue timeout",
                    },
                )
            else:
                attempt_started = time.monotonic()
                try:
                    result = await allocation.try_allocate(db, ticket=ticket, exclude_device_ids=excluded)
                except (CapabilityMergeError, RunNotActiveError) as e:
                    # try_allocate already cancelled the ticket; the begin context
                    # persists that. Put the descriptive message (merge error or
                    # inactive run) in the 400 body (wave-5 #26) — the router maps
                    # it to a W3C session-not-created. A re-poll of the cancelled
                    # ticket gets the generic text above.
                    terminal_response = JSONResponse(status_code=400, content={"status": "invalid", "message": str(e)})
                GRID_TRY_ALLOCATE_DURATION_SECONDS.observe(time.monotonic() - attempt_started)
        if terminal_response is not None:
            return terminal_response
        if result is not None:
            GRID_ALLOCATE_QUEUE_WAIT_SECONDS.labels(outcome="allocated").observe(time.monotonic() - started)
            claim_window = int(cast("int", services.settings.get("grid.claim_window_sec")))
            attempts += 1
            remaining = create_deadline - time.monotonic()
            outcome = await session_create.create_and_promote(
                services.session_factory,
                allocation,
                allocation=result,
                raw_body=raw_body,
                claim_window_sec=claim_window,
                max_create_timeout_sec=max(
                    0.0,
                    remaining - CREATE_TIMEOUT_MARGIN_SEC,
                ),
            )
            if outcome.kind in {"target_unreachable", "target_protocol_error"}:
                last_target_failure_message = outcome.message or None
                await session_create.mark_target_node_down(
                    services.session_factory,
                    services.health,
                    device_id=result.device_id,
                )
                excluded.add(result.device_id)
                remaining = create_deadline - time.monotonic()
                if attempts >= MAX_TARGET_ATTEMPTS or remaining < PER_ATTEMPT_MIN_BUDGET_SEC:
                    return CreateSessionResponse(status="create_error", message=outcome.message or None)
                continue
            return _response_for_outcome(outcome, result)
        now = time.monotonic()
        if now >= poll_deadline:
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="queued").inc()
            GRID_ALLOCATE_QUEUE_WAIT_SECONDS.labels(outcome="queued").observe(time.monotonic() - started)
            return CreateSessionResponse(status="queued", ticket=ticket_id)
        await asyncio.sleep(min(RETRY_INTERVAL_SEC, poll_deadline - now))


@router.delete("/tickets/{ticket_id}", status_code=204)
async def cancel_ticket(ticket_id: uuid.UUID, services: GridServicesDep) -> Response:
    async with services.session_factory.begin() as db:
        ticket = await db.get(GridSessionQueueTicket, ticket_id)
        if ticket is not None and ticket.status == GridQueueStatus.waiting:
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="router_cancelled")
    return Response(status_code=204)


@router.post("/sessions/ended", status_code=204)
async def ended(payload: EndedRequest, services: GridServicesDep) -> Response:
    await retry_on_serialization_failure(
        services.session_factory,
        lambda db: services.allocation.mark_ended(db, appium_session_id=payload.session_id),
        caller="grid_session_ended",
    )
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
async def activity(payload: ActivityRequest, services: GridServicesDep) -> Response:
    if not payload.sessions:
        return Response(status_code=204)
    # The payload means "these sessions were active"; stamp a single server-side
    # now() for every reported session (the legacy map form's caller datetimes were
    # always ignored — router clock skew would otherwise extend or defeat idle
    # reaping, which is judged against this host's clock). One UPDATE over an IN-set.
    now = now_utc()
    async with services.session_factory.begin() as db:
        await db.execute(
            update(Session)
            .where(Session.session_id.in_(payload.sessions), Session.status == SessionStatus.running)
            .values(last_activity_at=now)
        )
    return Response(status_code=204)
