import uuid
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import DbDep
from app.devices.routers.helpers import get_device_for_update_or_404, get_device_or_404
from app.devices.schemas.test_data import TestDataAuditEntryRead, TestDataPayload, TestDataRead
from app.devices.services import test_data as test_data_service

router = APIRouter(tags=["devices-test-data"])


@router.get("/{device_id}/test_data", response_model=TestDataRead)
async def get_test_data(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    return await test_data_service.get_device_test_data(db, device)


@router.put("/{device_id}/test_data", response_model=TestDataRead)
async def replace_test_data(
    device_id: uuid.UUID,
    payload: TestDataPayload,
    db: DbDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await test_data_service.replace_device_test_data(db, device, payload.root)


@router.patch("/{device_id}/test_data", response_model=TestDataRead)
async def merge_test_data(
    device_id: uuid.UUID,
    payload: TestDataPayload,
    db: DbDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await test_data_service.merge_device_test_data(db, device, payload.root)


@router.get("/{device_id}/test_data/history", response_model=list[TestDataAuditEntryRead])
async def get_history(
    device_id: uuid.UUID,
    db: DbDep,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    await get_device_or_404(device_id, db)
    logs = await test_data_service.get_test_data_history(db, device_id, limit=limit)
    return [
        {
            "id": str(log.id),
            "previous_test_data": log.previous_test_data,
            "new_test_data": log.new_test_data,
            "changed_by": log.changed_by,
            "changed_at": log.changed_at.isoformat(),
        }
        for log in logs
    ]
