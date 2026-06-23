from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409
from app.core.http_errors import found_or_404
from app.devices.dependencies import DeviceServicesDep

# build_device_query_filters is a FastAPI dependency (not a route handler, so it is not
# covered by runtime-evaluated-decorators); FastAPI resolves its Query-param enum
# annotations at runtime via get_type_hints, so these must stay at module scope.
from app.devices.models import ConnectionType, DeviceType, HardwareHealthStatus  # noqa: TC001
from app.devices.routers.helpers import get_device_or_404
from app.devices.schemas.device import (
    DeviceDetail,
    DevicePatch,
    DeviceRead,
    HardwareTelemetryState,
    SessionOutcomeHeatmapRow,
)
from app.devices.schemas.filters import (
    ChipStatus,
    DeviceQueryFilters,
    DeviceSortBy,
    DeviceSortDir,
    HealthVerdictFilter,
)
from app.devices.services import (
    health as device_health,
)
from app.devices.services import (
    identity_conflicts,
)
from app.devices.services import (
    platform_label as platform_label_service,
)
from app.runs import service as run_service
from app.sessions.dependencies import SessionServicesDep

DeviceIdentityConflictError = identity_conflicts.DeviceIdentityConflictError

DEVICE_CORE_ERROR_RESPONSES = {**RESPONSES_400, **RESPONSES_401, **RESPONSES_404, **RESPONSES_409}

router = APIRouter(responses=DEVICE_CORE_ERROR_RESPONSES)


def _extract_tag_filters(request: Request) -> dict[str, str] | None:
    tags = {
        key.removeprefix("tags."): value
        for key, value in request.query_params.multi_items()
        if key.startswith("tags.") and key != "tags."
    }
    return tags or None


def build_device_query_filters(
    request: Request,
    pack_id: str | None = Query(None),
    platform_id: str | None = Query(None),
    status: ChipStatus | None = Query(None),
    reserved: bool | None = Query(None),
    host_id: uuid.UUID | None = Query(None),
    identity_value: str | None = Query(None),
    connection_target: str | None = Query(None),
    device_type: DeviceType | None = Query(None),
    connection_type: ConnectionType | None = Query(None),
    os_version: str | None = Query(None),
    os_version_display: str | None = Query(None),
    search: str | None = Query(None),
    hardware_health_status: HardwareHealthStatus | None = Query(None),
    hardware_telemetry_state: HardwareTelemetryState | None = Query(None),
    needs_attention: bool | None = Query(None),
    device_health: HealthVerdictFilter | None = Query(None),
    node_health: HealthVerdictFilter | None = Query(None),
    viability: HealthVerdictFilter | None = Query(None),
    sort_by: DeviceSortBy = Query("created_at"),
    sort_dir: DeviceSortDir = Query("desc"),
) -> DeviceQueryFilters:
    return DeviceQueryFilters(
        pack_id=pack_id,
        platform_id=platform_id,
        status=status,
        reserved=reserved,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=connection_target,
        device_type=device_type,
        connection_type=connection_type,
        os_version=os_version,
        os_version_display=os_version_display,
        search=search,
        hardware_health_status=hardware_health_status,
        hardware_telemetry_state=hardware_telemetry_state,
        needs_attention=needs_attention,
        device_health=device_health,
        node_health=node_health,
        viability=viability,
        sort_by=sort_by,
        sort_dir=sort_dir,
        tags=_extract_tag_filters(request),
    )


DeviceFiltersDep = Annotated[DeviceQueryFilters, Depends(build_device_query_filters)]


@router.get("")
async def list_devices(
    filters: DeviceFiltersDep,
    db: DbDep,
    device_services: DeviceServicesDep,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    if limit is not None:
        effective_offset = offset if offset is not None else 0
        devices, total = await device_services.crud.list_devices_paginated(db, filters, limit, effective_offset)
    else:
        devices = await device_services.crud.list_devices_by_filters(db, filters)
        total = None

    reservation_map = await run_service.get_device_reservation_map(db, [device.id for device in devices])
    health_summary_map = {str(device.id): device_health.build_public_summary(device) for device in devices}
    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in devices),
    )
    serialization_contexts = await device_services.presenter.build_serialization_contexts(db, devices)
    serialized: list[dict[str, Any]] = []
    for device in devices:
        reservation_context = run_service.get_reservation_context_for_device(reservation_map.get(device.id), device.id)
        payload = await device_services.presenter.serialize_device(
            db,
            device,
            reservation_context=reservation_context,
            health_summary=health_summary_map.get(str(device.id)),
            platform_label=label_map.get((device.pack_id, device.platform_id)),
            precomputed=serialization_contexts[device.id],
        )
        serialized.append(payload)

    if total is not None:
        return {
            "items": serialized,
            "total": total,
            "limit": limit,
            "offset": effective_offset,
        }
    return serialized


@router.get("/{device_id}", response_model=DeviceDetail)
async def get_device(device_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)
    platform_label = await platform_label_service.load_platform_label(
        db,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
    )
    return await device_services.presenter.serialize_device_detail(
        db,
        device,
        health_summary=device_health.build_public_summary(device),
        platform_label=platform_label,
    )


@router.get("/{device_id}/capabilities")
async def device_capabilities(device_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)
    return await device_services.capability.get_device_capabilities(db, device)


@router.patch("/{device_id}", response_model=DeviceRead)
async def update_device(
    device_id: uuid.UUID, data: DevicePatch, db: DbDep, device_services: DeviceServicesDep
) -> dict[str, Any]:
    try:
        device = await device_services.crud.update_device(db, device_id, data)
    except DeviceIdentityConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    device = found_or_404(device, "Device not found")
    return await device_services.presenter.serialize_device(db, device)


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> None:
    deleted = await device_services.crud.delete_device(db, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")


@router.get("/{device_id}/session-outcome-heatmap", response_model=list[SessionOutcomeHeatmapRow])
async def device_session_outcome_heatmap(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    session_services: SessionServicesDep,
    days: Annotated[int, Query(ge=1, le=90)] = 90,
) -> list[SessionOutcomeHeatmapRow]:
    await get_device_or_404(device_id, db, device_services.crud)
    rows = await session_services.crud.get_device_session_outcome_heatmap_rows(db, device_id, days=days)
    return [SessionOutcomeHeatmapRow(timestamp=timestamp, status=status) for timestamp, status in rows]
