from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404, RESPONSES_409, RESPONSES_422
from app.core.http_errors import found_or_404
from app.core.pagination import CursorPaginationError
from app.devices import schemas as device_schemas
from app.devices.services import platform_label as platform_label_service
from app.sessions import service_kill
from app.sessions.dependencies import SessionServicesDep
from app.sessions.filters import SessionFilters
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SessionDetail = device_schemas.SessionDetail
SessionListRead = device_schemas.SessionListRead
SessionRead = device_schemas.SessionRead
SessionStatusUpdate = device_schemas.SessionStatusUpdate
SessionKillResult = device_schemas.SessionKillResult

SESSION_ERROR_RESPONSES = {**RESPONSES_401, **RESPONSES_404, **RESPONSES_409, **RESPONSES_422}

router = APIRouter(prefix="/api/sessions", tags=["sessions"], responses=SESSION_ERROR_RESPONSES)


async def _session_details_with_labels(db: AsyncSession, sessions: list[Session]) -> list[SessionDetail]:
    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((session.device.pack_id, session.device.platform_id) for session in sessions if session.device is not None),
    )
    return [
        SessionDetail.from_session(
            session,
            device_platform_label=(
                label_map.get((session.device.pack_id, session.device.platform_id))
                if session.device is not None
                else None
            ),
        )
        for session in sessions
    ]


@router.get("", response_model=SessionListRead)
async def list_sessions(
    request: Request,
    db: DbDep,
    session_services: SessionServicesDep,
    device_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[SessionStatus | None, Query()] = None,
    pack_id: Annotated[str | None, Query()] = None,
    platform_id: Annotated[str | None, Query()] = None,
    started_after: Annotated[datetime | None, Query()] = None,
    started_before: Annotated[datetime | None, Query()] = None,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    direction: Annotated[Literal["older", "newer"], Query()] = "older",
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[
        Literal["session_id", "device", "test_name", "platform", "started_at", "duration", "status"], Query()
    ] = "started_at",
    sort_dir: Annotated[Literal["asc", "desc"], Query()] = "desc",
    include_probes: Annotated[bool, Query()] = False,
    active: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    filters = SessionFilters(
        device_id=device_id,
        status=status,
        pack_id=pack_id,
        platform_id=platform_id,
        started_after=started_after,
        started_before=started_before,
        run_id=run_id,
        active=active,
    )
    cursor_mode = "cursor" in request.query_params or "direction" in request.query_params
    if cursor_mode:
        try:
            page = await session_services.crud.list_sessions_cursor(
                db,
                filters=filters,
                limit=limit,
                cursor=cursor,
                direction=direction,
                include_probes=include_probes,
            )
        except CursorPaginationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "items": await _session_details_with_labels(db, page.items),
            "limit": page.limit,
            "next_cursor": page.next_cursor,
            "prev_cursor": page.prev_cursor,
        }
    sessions, total = await session_services.crud.list_sessions(
        db,
        filters=filters,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
        include_probes=include_probes,
    )
    return {
        "items": await _session_details_with_labels(db, sessions),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, db: DbDep, session_services: SessionServicesDep) -> SessionDetail:
    session = found_or_404(await session_services.crud.get_session(db, session_id), "Session not found")
    details = await _session_details_with_labels(db, [session])
    return details[0]


@router.post("/{session_id}/kill", response_model=SessionKillResult)
async def kill_session(
    session_id: str,
    db: DbDep,
    session_services: SessionServicesDep,
) -> SessionKillResult:
    try:
        outcome = await service_kill.kill_session(db, crud=session_services.crud, session_id=session_id)
    except service_kill.SessionNotKillableError:
        raise HTTPException(status_code=409, detail="Session is not running") from None
    outcome = found_or_404(outcome, "Session not found")
    return SessionKillResult(
        terminated=outcome.terminated,
        session=SessionRead.model_validate(outcome.session),
    )


@router.patch("/{session_id}/status", response_model=SessionRead)
async def update_session_status(
    session_id: str,
    data: SessionStatusUpdate,
    db: DbDep,
    session_services: SessionServicesDep,
) -> Session:
    return found_or_404(
        await session_services.crud.update_session_status(db, session_id, data.status), "Session not found"
    )
