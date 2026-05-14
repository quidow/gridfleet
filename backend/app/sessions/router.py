import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import DbDep
from app.core.pagination import CursorPaginationError
from app.devices import schemas as device_schemas
from app.devices.services import platform_label as platform_label_service
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus

SessionCreate = device_schemas.SessionCreate
SessionDetail = device_schemas.SessionDetail
SessionListRead = device_schemas.SessionListRead
SessionRead = device_schemas.SessionRead
SessionStatusUpdate = device_schemas.SessionStatusUpdate

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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
    device_id: uuid.UUID | None = Query(None),
    status: SessionStatus | None = Query(None),
    pack_id: str | None = Query(None),
    platform_id: str | None = Query(None),
    started_after: datetime | None = Query(None),
    started_before: datetime | None = Query(None),
    run_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    direction: Literal["older", "newer"] = Query("older"),
    offset: int = Query(0, ge=0),
    sort_by: Literal["session_id", "device", "test_name", "platform", "started_at", "duration", "status"] = Query(
        "started_at"
    ),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
) -> SessionListRead:
    cursor_mode = "cursor" in request.query_params or "direction" in request.query_params
    if cursor_mode:
        try:
            page = await session_service.list_sessions_cursor(
                db,
                device_id=device_id,
                status=status,
                pack_id=pack_id,
                platform_id=platform_id,
                started_after=started_after,
                started_before=started_before,
                run_id=run_id,
                limit=limit,
                cursor=cursor,
                direction=direction,
            )
        except CursorPaginationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return SessionListRead(
            items=await _session_details_with_labels(db, page.items),
            limit=page.limit,
            next_cursor=page.next_cursor,
            prev_cursor=page.prev_cursor,
        )
    sessions, total = await session_service.list_sessions(
        db,
        device_id=device_id,
        status=status,
        pack_id=pack_id,
        platform_id=platform_id,
        started_after=started_after,
        started_before=started_before,
        run_id=run_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return SessionListRead(
        items=await _session_details_with_labels(db, sessions),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, db: DbDep) -> SessionDetail:
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    details = await _session_details_with_labels(db, [session])
    return details[0]


@router.post("", response_model=SessionRead)
async def register_session(
    data: SessionCreate,
    db: DbDep,
) -> Session:
    try:
        return await session_service.register_session(
            db,
            session_id=data.session_id,
            test_name=data.test_name,
            device_id=data.device_id,
            connection_target=data.connection_target,
            status=data.status,
            requested_pack_id=data.requested_pack_id,
            requested_platform_id=data.requested_platform_id,
            requested_device_type=data.requested_device_type,
            requested_connection_type=data.requested_connection_type,
            requested_capabilities=data.requested_capabilities,
            error_type=data.error_type,
            error_message=data.error_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{session_id}/status", response_model=SessionRead)
async def update_session_status(
    session_id: str,
    data: SessionStatusUpdate,
    db: DbDep,
) -> Session:
    session = await session_service.update_session_status(db, session_id, data.status)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/{session_id}/finished", status_code=204)
async def post_session_finished(
    session_id: str,
    db: DbDep,
) -> Response:
    result = await session_service.mark_session_finished(db, session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return Response(status_code=204)
