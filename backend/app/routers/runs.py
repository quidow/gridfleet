import uuid
from datetime import UTC, date, datetime, time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.models.device import Device
from app.models.test_run import RunState
from app.schemas.run import (
    HeartbeatResponse,
    ReservedDeviceInfo,
    RunCreate,
    RunCreateResponse,
    RunDetail,
    RunListRead,
    RunPreparationFailureReport,
    RunRead,
)
from app.services import run_service
from app.services.cursor_pagination import CursorPaginationError
from app.services.settings_service import settings_service

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _parse_run_filter_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None

    parsed: datetime
    if len(value) == 10:
        parsed_date = date.fromisoformat(value)
        parsed = datetime.combine(parsed_date, time.max if end_of_day else time.min, tzinfo=UTC)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.post("", response_model=RunCreateResponse, status_code=201)
async def create_run(
    data: RunCreate,
    include: str | None = Query(
        None, description="Comma-separated: config,test_data (capabilities not supported on reserve)"
    ),
    db: AsyncSession = Depends(get_db),
) -> RunCreateResponse:
    includes = run_service.parse_includes(include, allowed={"config", "capabilities", "test_data"})
    if "capabilities" in includes:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "reserve_capabilities_unsupported",
                "message": (
                    "include=capabilities is not supported on reserve; reserved devices may not be"
                    " online and capabilities require a live agent probe"
                ),
            },
        )

    try:
        run, device_infos = await run_service.create_run(db, data)
    except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    if includes:
        device_ids = [uuid.UUID(info.device_id) for info in device_infos]
        devices = (
            (
                await db.execute(
                    select(Device).options(selectinload(Device.appium_node)).where(Device.id.in_(device_ids))
                )
            )
            .scalars()
            .all()
        )
        device_by_id = {str(d.id): d for d in devices}
        pairs = []
        for info in device_infos:
            device = device_by_id.get(info.device_id)
            if device is None:
                run_service.mark_reserved_device_info_includes_unavailable(
                    info,
                    includes=includes,
                    reason="device_not_found",
                )
                continue
            pairs.append((info, device))
        await run_service.hydrate_reserved_device_infos(db, pairs, includes=includes)

    return RunCreateResponse(
        id=run.id,
        name=run.name,
        state=run.state,
        devices=device_infos,
        grid_url=settings_service.get("grid.hub_url"),
        ttl_minutes=run.ttl_minutes,
        heartbeat_timeout_sec=run.heartbeat_timeout_sec,
        created_at=run.created_at,
    )


@router.get("", response_model=RunListRead)
async def list_runs(
    request: Request,
    state: RunState | None = Query(None),
    created_from: str | None = Query(None),
    created_to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    direction: Literal["older", "newer"] = Query("older"),
    offset: int = Query(0, ge=0),
    sort_by: Literal["name", "state", "devices", "created_by", "created_at", "duration"] = Query("created_at"),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
    db: AsyncSession = Depends(get_db),
) -> RunListRead:
    try:
        parsed_created_from = _parse_run_filter_datetime(created_from)
        parsed_created_to = _parse_run_filter_datetime(created_to, end_of_day=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date filter: {e}") from e
    cursor_mode = "cursor" in request.query_params or "direction" in request.query_params
    if cursor_mode:
        try:
            page = await run_service.list_runs_cursor(
                db,
                state=state,
                created_from=parsed_created_from,
                created_to=parsed_created_to,
                limit=limit,
                cursor=cursor,
                direction=direction,
            )
        except CursorPaginationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        counts_map = await run_service.fetch_session_counts(db, [r.id for r in page.items])
        items = [run_service.build_run_read(r, counts_map.get(r.id)) for r in page.items]
        return RunListRead(
            items=items,
            limit=page.limit,
            next_cursor=page.next_cursor,
            prev_cursor=page.prev_cursor,
        )
    runs, total = await run_service.list_runs(
        db,
        state=state,
        created_from=parsed_created_from,
        created_to=parsed_created_to,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    counts_map = await run_service.fetch_session_counts(db, [r.id for r in runs])
    items = [run_service.build_run_read(r, counts_map.get(r.id)) for r in runs]
    return RunListRead(items=items, total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunDetail:
    run = await run_service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    devices = [ReservedDeviceInfo(**d) for d in (run.reserved_devices or [])]
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    base = run_service.build_run_read(run, counts_map.get(run.id))
    return RunDetail(
        **base.model_dump(),
        devices=devices,
    )


@router.post("/{run_id}/ready", response_model=RunRead)
async def signal_ready(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunRead:
    try:
        run = await run_service.signal_ready(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/active", response_model=RunRead)
async def signal_active(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunRead:
    try:
        run = await run_service.signal_active(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/devices/{device_id}/preparation-failed", response_model=RunRead)
async def report_preparation_failed(
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    payload: RunPreparationFailureReport,
    db: AsyncSession = Depends(get_db),
) -> RunRead:
    try:
        run = await run_service.report_preparation_failure(
            db,
            run_id,
            device_id,
            message=payload.message,
            source=payload.source,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> HeartbeatResponse:
    try:
        run = await run_service.heartbeat(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return HeartbeatResponse(state=run.state, last_heartbeat=run.last_heartbeat)


@router.post("/{run_id}/complete", response_model=RunRead)
async def complete_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunRead:
    try:
        run = await run_service.complete_run(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/cancel", response_model=RunRead)
async def cancel_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunRead:
    try:
        run = await run_service.cancel_run(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/force-release", response_model=RunRead)
async def force_release(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> RunRead:
    try:
        run = await run_service.force_release(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    counts_map = await run_service.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))
