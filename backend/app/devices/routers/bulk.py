from typing import Any

from fastapi import APIRouter

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409
from app.devices.schemas.device import (
    BulkDeviceIds,
    BulkMaintenanceEnter,
    BulkOperationResult,
    BulkTagsUpdate,
)
from app.devices.services import bulk as bulk_service
from app.events.dependencies import EventServicesDep
from app.settings.dependencies import SettingsServicesDep

DEVICE_BULK_ERROR_RESPONSES = {**RESPONSES_400, **RESPONSES_401, **RESPONSES_404, **RESPONSES_409}

router = APIRouter(prefix="/api/devices/bulk", tags=["bulk"], responses=DEVICE_BULK_ERROR_RESPONSES)


@router.post("/start-nodes", response_model=BulkOperationResult)
async def bulk_start_nodes(
    body: BulkDeviceIds, db: DbDep, events: EventServicesDep, settings_services: SettingsServicesDep
) -> dict[str, Any]:
    return await bulk_service.bulk_start_nodes(
        db, body.device_ids, publisher=events.publisher, settings=settings_services.reader
    )


@router.post("/stop-nodes", response_model=BulkOperationResult)
async def bulk_stop_nodes(body: BulkDeviceIds, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_stop_nodes(db, body.device_ids, publisher=events.publisher)


@router.post("/restart-nodes", response_model=BulkOperationResult)
async def bulk_restart_nodes(
    body: BulkDeviceIds, db: DbDep, events: EventServicesDep, settings_services: SettingsServicesDep
) -> dict[str, Any]:
    return await bulk_service.bulk_restart_nodes(
        db, body.device_ids, publisher=events.publisher, settings=settings_services.reader
    )


@router.post("/update-tags", response_model=BulkOperationResult)
async def bulk_update_tags(body: BulkTagsUpdate, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_update_tags(db, body.device_ids, body.tags, body.merge, publisher=events.publisher)


@router.post("/delete", response_model=BulkOperationResult)
async def bulk_delete(body: BulkDeviceIds, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_delete(db, body.device_ids, publisher=events.publisher)


@router.post("/enter-maintenance", response_model=BulkOperationResult)
async def bulk_enter_maintenance(body: BulkMaintenanceEnter, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_enter_maintenance(db, body.device_ids, publisher=events.publisher)


@router.post("/exit-maintenance", response_model=BulkOperationResult)
async def bulk_exit_maintenance(body: BulkDeviceIds, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_exit_maintenance(db, body.device_ids, publisher=events.publisher)


@router.post("/reconnect", response_model=BulkOperationResult)
async def bulk_reconnect(body: BulkDeviceIds, db: DbDep, events: EventServicesDep) -> dict[str, Any]:
    return await bulk_service.bulk_reconnect(db, body.device_ids, publisher=events.publisher)
