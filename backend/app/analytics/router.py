from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.analytics import service as analytics_service
from app.analytics.schemas import (
    DeviceReliabilityRow,
    DeviceUtilizationRow,
    FleetCapacityTimeline,
    FleetOverview,
    GroupByOption,
    SessionSummaryRow,
)
from app.core.csv_export import to_csv_response
from app.core.dependencies import DbDep
from app.core.timeutil import now_utc
from app.devices.dependencies import DeviceServicesDep

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _default_date_from() -> datetime:
    return now_utc() - timedelta(days=30)


def _default_capacity_date_from() -> datetime:
    return now_utc() - timedelta(hours=24)


@router.get("/sessions/summary", response_model=list[SessionSummaryRow])
async def session_summary(
    db: DbDep,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    group_by: Annotated[GroupByOption, Query()] = GroupByOption.day,
    export_format: Annotated[str | None, Query(alias="format")] = None,
) -> list[SessionSummaryRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or now_utc()
    rows = await analytics_service.get_session_summary(db, df, dt, group_by)
    if export_format == "csv":
        return to_csv_response(rows, "session_summary.csv")
    return rows


@router.get("/devices/utilization", response_model=list[DeviceUtilizationRow])
async def device_utilization(
    db: DbDep,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    export_format: Annotated[str | None, Query(alias="format")] = None,
) -> list[DeviceUtilizationRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or now_utc()
    rows = await analytics_service.get_device_utilization(db, df, dt)
    if export_format == "csv":
        return to_csv_response(rows, "device_utilization.csv")
    return rows


@router.get("/devices/reliability", response_model=list[DeviceReliabilityRow])
async def device_reliability(
    db: DbDep,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    export_format: Annotated[str | None, Query(alias="format")] = None,
) -> list[DeviceReliabilityRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or now_utc()
    rows = await analytics_service.get_device_reliability(db, df, dt)
    if export_format == "csv":
        return to_csv_response(rows, "device_reliability.csv")
    return rows


@router.get("/fleet/overview", response_model=FleetOverview)
async def fleet_overview(
    db: DbDep,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> FleetOverview:
    df = date_from or _default_date_from()
    dt = date_to or now_utc()
    return await analytics_service.get_fleet_overview(db, df, dt)


@router.get("/fleet/capacity-timeline", response_model=FleetCapacityTimeline)
async def fleet_capacity_timeline(
    db: DbDep,
    device_services: DeviceServicesDep,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 1,
) -> FleetCapacityTimeline:
    df = date_from or _default_capacity_date_from()
    dt = date_to or now_utc()
    try:
        return await device_services.fleet_capacity.get_fleet_capacity_timeline(
            db, date_from=df, date_to=dt, bucket_minutes=bucket_minutes
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
