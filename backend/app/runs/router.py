import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.agent_comm.reconfigure_delivery import InlineReconfigureDeliveryFailedError
from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409, RESPONSES_422
from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.core.http_errors import found_or_404
from app.core.pagination import CursorPaginationError
from app.runs import service as run_service
from app.runs.dependencies import RunServicesDep
from app.runs.models import RunState
from app.runs.schemas import (
    HeartbeatResponse,
    ReservedDeviceInfo,
    RunCooldownEscalatedResponse,
    RunCooldownRequest,
    RunCooldownResponse,
    RunCreate,
    RunCreateResponse,
    RunDetail,
    RunListRead,
    RunPreparationFailureReport,
    RunRead,
)

RUN_ERROR_RESPONSES = {**RESPONSES_400, **RESPONSES_401, **RESPONSES_404, **RESPONSES_409, **RESPONSES_422}

router = APIRouter(prefix="/api/runs", tags=["runs"], responses=RUN_ERROR_RESPONSES)

# Length of a bare "YYYY-MM-DD" date (no time component).
_ISO_DATE_LENGTH = 10


def _parse_run_filter_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None

    parsed: datetime
    if len(value) == _ISO_DATE_LENGTH:
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
    db: DbDep,
    run_services: RunServicesDep,
) -> dict[str, Any]:
    try:
        run, device_infos = await run_services.allocator.create_run(db, data)
    except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return {
        "id": run.id,
        "name": run.name,
        "state": run.state,
        "devices": device_infos,
        "ttl_minutes": run.ttl_minutes,
        "heartbeat_timeout_sec": run.heartbeat_timeout_sec,
        "created_at": run.created_at,
    }


@router.get("", response_model=RunListRead)
async def list_runs(
    request: Request,
    db: DbDep,
    run_services: RunServicesDep,
    state: Annotated[RunState | None, Query()] = None,
    created_from: Annotated[str | None, Query()] = None,
    created_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    direction: Annotated[Literal["older", "newer"], Query()] = "older",
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[
        Literal["name", "state", "devices", "created_by", "created_at", "duration"], Query()
    ] = "created_at",
    sort_dir: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> dict[str, Any]:
    try:
        parsed_created_from = _parse_run_filter_datetime(created_from)
        parsed_created_to = _parse_run_filter_datetime(created_to, end_of_day=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date filter: {e}") from e
    cursor_mode = "cursor" in request.query_params or "direction" in request.query_params
    if cursor_mode:
        try:
            page = await run_services.query.list_runs_cursor(
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
        counts_map = await run_services.query.fetch_session_counts(db, [r.id for r in page.items])
        items = [run_service.build_run_read(r, counts_map.get(r.id)) for r in page.items]
        return {
            "items": items,
            "limit": page.limit,
            "next_cursor": page.next_cursor,
            "prev_cursor": page.prev_cursor,
        }
    runs, total = await run_services.query.list_runs(
        db,
        state=state,
        created_from=parsed_created_from,
        created_to=parsed_created_to,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    counts_map = await run_services.query.fetch_session_counts(db, [r.id for r in runs])
    items = [run_service.build_run_read(r, counts_map.get(r.id)) for r in runs]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, db: DbDep, run_services: RunServicesDep) -> dict[str, Any]:
    run = found_or_404(await run_service.get_run(db, run_id), "Run not found")
    devices = [ReservedDeviceInfo(**d) for d in (run.reserved_devices or [])]
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    base = run_service.build_run_read(run, counts_map.get(run.id))
    return {**base.model_dump(), "devices": devices}


@router.post("/{run_id}/ready", response_model=RunRead)
async def signal_ready(run_id: uuid.UUID, db: DbDep, run_services: RunServicesDep) -> RunRead:
    try:
        run = await run_services.lifecycle.signal_ready(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/active", response_model=RunRead)
async def signal_active(run_id: uuid.UUID, db: DbDep, run_services: RunServicesDep) -> RunRead:
    try:
        run = await run_services.lifecycle.signal_active(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/devices/{device_id}/preparation-failed", response_model=RunRead)
async def report_preparation_failed(
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    payload: RunPreparationFailureReport,
    db: DbDep,
    run_services: RunServicesDep,
) -> RunRead:
    try:
        run = await run_services.failure.report_preparation_failure(
            db,
            run_id,
            device_id,
            message=payload.message,
            source=payload.source,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/devices/{device_id}/cooldown", status_code=200)
async def cooldown_device_endpoint(
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    payload: RunCooldownRequest,
    db: DbDep,
    run_services: RunServicesDep,
) -> RunCooldownResponse | RunCooldownEscalatedResponse:
    try:
        (
            excluded_until,
            cooldown_count,
            escalated,
            threshold,
            entered_maintenance,
        ) = await run_services.failure.cooldown_device(
            db,
            run_id,
            device_id,
            reason=payload.reason,
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        if "ttl_seconds must be <=" in msg:
            raise HTTPException(status_code=422, detail=msg) from e
        raise HTTPException(status_code=409, detail=msg) from e
    except InlineReconfigureDeliveryFailedError as e:
        # The DB state has the device on cooldown (intents committed,
        # reservation marked excluded), but the agent-side drain did not
        # land. Returning 503 with ``Retry-After`` tells testkit not to
        # assume the device is safely out of rotation — the next session
        # request could still land on it. The background delivery loop
        # will retry the drain; an operator-facing health check will
        # surface a stuck outbox row if the agent is wedged.
        raise HTTPException(
            status_code=503,
            detail=f"cooldown applied in manager but agent drain failed: {e}",
            headers={"Retry-After": "5"},
        ) from e

    if escalated:
        return RunCooldownEscalatedResponse(
            status="maintenance_escalated" if entered_maintenance else "released",
            cooldown_count=cooldown_count,
            threshold=threshold,
        )
    if excluded_until is None:
        raise HTTPException(status_code=500, detail="Cooldown returned no expiry")
    return RunCooldownResponse(
        status="cooldown_set",
        excluded_until=excluded_until,
        cooldown_count=cooldown_count,
    )


@router.post("/{run_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(run_id: uuid.UUID, db: DbDep, run_services: RunServicesDep) -> dict[str, Any]:
    try:
        run = await run_services.lifecycle.heartbeat(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"state": run.state, "last_heartbeat": run.last_heartbeat}


@router.post("/{run_id}/complete", response_model=RunRead)
async def complete_run(
    run_id: uuid.UUID,
    db: DbDep,
    run_services: RunServicesDep,
) -> RunRead:
    try:
        run = await run_services.lifecycle.complete_run(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/cancel", response_model=RunRead)
async def cancel_run(
    run_id: uuid.UUID,
    db: DbDep,
    run_services: RunServicesDep,
) -> RunRead:
    try:
        run = await run_services.lifecycle.cancel_run(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))


@router.post("/{run_id}/force-release", response_model=RunRead)
async def force_release(
    run_id: uuid.UUID,
    db: DbDep,
    run_services: RunServicesDep,
) -> RunRead:
    try:
        run = await run_services.lifecycle.force_release(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    counts_map = await run_services.query.fetch_session_counts(db, [run.id])
    return run_service.build_run_read(run, counts_map.get(run.id))
