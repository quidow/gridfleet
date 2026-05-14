from typing import Any

from fastapi import APIRouter

from app.core.dependencies import DbDep
from app.devices.schemas.device import (
    BulkAutoManageUpdate,
    BulkDeviceIds,
    BulkMaintenanceEnter,
    BulkOperationResult,
    BulkTagsUpdate,
)
from app.devices.services import bulk as bulk_service

router = APIRouter(prefix="/api/devices/bulk", tags=["bulk"])


@router.post("/start-nodes", response_model=BulkOperationResult)
async def bulk_start_nodes(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_start_nodes(db, body.device_ids)


@router.post("/stop-nodes", response_model=BulkOperationResult)
async def bulk_stop_nodes(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_stop_nodes(db, body.device_ids)


@router.post("/restart-nodes", response_model=BulkOperationResult)
async def bulk_restart_nodes(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_restart_nodes(db, body.device_ids)


@router.post("/set-auto-manage", response_model=BulkOperationResult)
async def bulk_set_auto_manage(body: BulkAutoManageUpdate, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_set_auto_manage(db, body.device_ids, body.auto_manage)


@router.post("/update-tags", response_model=BulkOperationResult)
async def bulk_update_tags(body: BulkTagsUpdate, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_update_tags(db, body.device_ids, body.tags, body.merge)


@router.post("/delete", response_model=BulkOperationResult)
async def bulk_delete(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_delete(db, body.device_ids)


@router.post("/enter-maintenance", response_model=BulkOperationResult)
async def bulk_enter_maintenance(body: BulkMaintenanceEnter, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_enter_maintenance(db, body.device_ids)


@router.post("/exit-maintenance", response_model=BulkOperationResult)
async def bulk_exit_maintenance(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_exit_maintenance(db, body.device_ids)


@router.post("/reconnect", response_model=BulkOperationResult)
async def bulk_reconnect(body: BulkDeviceIds, db: DbDep) -> dict[str, Any]:
    return await bulk_service.bulk_reconnect(db, body.device_ids)
