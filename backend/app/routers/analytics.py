from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.analytics import (
    DeviceReliabilityRow,
    DeviceUtilizationRow,
    FleetCapacityTimeline,
    FleetOverview,
    GroupByOption,
    SessionSummaryRow,
)
from app.services import analytics_service
from app.services.csv_export import to_csv_response
from app.services.fleet_capacity import get_fleet_capacity_timeline

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _default_date_from() -> datetime:
    return datetime.now(UTC) - timedelta(days=30)


def _default_date_to() -> datetime:
    return datetime.now(UTC)


def _default_capacity_date_from() -> datetime:
    return datetime.now(UTC) - timedelta(hours=24)


@router.get("/sessions/summary", response_model=list[SessionSummaryRow])
async def session_summary(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    group_by: GroupByOption = Query(GroupByOption.day),
    export_format: str | None = Query(None, alias="format"),
    db: AsyncSession = Depends(get_db),
) -> list[SessionSummaryRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or _default_date_to()
    rows = await analytics_service.get_session_summary(db, df, dt, group_by)
    if export_format == "csv":
        return to_csv_response(rows, "session_summary.csv")
    return rows


@router.get("/devices/utilization", response_model=list[DeviceUtilizationRow])
async def device_utilization(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    export_format: str | None = Query(None, alias="format"),
    db: AsyncSession = Depends(get_db),
) -> list[DeviceUtilizationRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or _default_date_to()
    rows = await analytics_service.get_device_utilization(db, df, dt)
    if export_format == "csv":
        return to_csv_response(rows, "device_utilization.csv")
    return rows


@router.get("/devices/reliability", response_model=list[DeviceReliabilityRow])
async def device_reliability(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    export_format: str | None = Query(None, alias="format"),
    db: AsyncSession = Depends(get_db),
) -> list[DeviceReliabilityRow] | StreamingResponse:
    df = date_from or _default_date_from()
    dt = date_to or _default_date_to()
    rows = await analytics_service.get_device_reliability(db, df, dt)
    if export_format == "csv":
        return to_csv_response(rows, "device_reliability.csv")
    return rows


@router.get("/fleet/overview", response_model=FleetOverview)
async def fleet_overview(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> FleetOverview:
    df = date_from or _default_date_from()
    dt = date_to or _default_date_to()
    return await analytics_service.get_fleet_overview(db, df, dt)


@router.get("/fleet/capacity-timeline", response_model=FleetCapacityTimeline)
async def fleet_capacity_timeline(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    bucket_minutes: int = Query(1, ge=1, le=1440),
    db: AsyncSession = Depends(get_db),
) -> FleetCapacityTimeline:
    df = date_from or _default_capacity_date_from()
    dt = date_to or _default_date_to()
    try:
        return await get_fleet_capacity_timeline(db, date_from=df, date_to=dt, bucket_minutes=bucket_minutes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
