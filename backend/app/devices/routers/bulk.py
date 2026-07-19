from typing import Any

from fastapi import APIRouter

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.devices.dependencies import DeviceServicesDep
from app.devices.schemas.device import (
    BulkDeviceIds,
    BulkOperationResult,
)

DEVICE_BULK_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/devices/bulk", tags=["bulk"], responses=DEVICE_BULK_ERROR_RESPONSES)


@router.post("/start-nodes", response_model=BulkOperationResult)
async def bulk_start_nodes(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_start_nodes(db, body.device_ids)


@router.post("/stop-nodes", response_model=BulkOperationResult)
async def bulk_stop_nodes(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_stop_nodes(db, body.device_ids)


@router.post("/restart-nodes", response_model=BulkOperationResult)
async def bulk_restart_nodes(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_restart_nodes(db, body.device_ids)


@router.post("/delete", response_model=BulkOperationResult)
async def bulk_delete(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_delete(db, body.device_ids)


@router.post("/enter-maintenance", response_model=BulkOperationResult)
async def bulk_enter_maintenance(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_enter_maintenance(db, body.device_ids)


@router.post("/exit-maintenance", response_model=BulkOperationResult)
async def bulk_exit_maintenance(body: BulkDeviceIds, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    return await device_services.bulk.bulk_exit_maintenance(db, body.device_ids)


@router.post("/reconnect", response_model=BulkOperationResult)
async def bulk_reconnect(
    body: BulkDeviceIds,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    return await device_services.bulk.bulk_reconnect(db, body.device_ids)
