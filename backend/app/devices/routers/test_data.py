import uuid
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404
from app.devices.dependencies import DeviceServicesDep
from app.devices.routers.helpers import get_device_for_update_or_404, get_device_or_404
from app.devices.schemas.test_data import TestDataAuditEntryRead, TestDataPayload, TestDataRead

router = APIRouter(tags=["devices-test-data"], responses={**RESPONSES_400, **RESPONSES_401, **RESPONSES_404})


@router.get("/{device_id}/test_data", response_model=TestDataRead)
async def get_test_data(device_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    return await device_services.test_data.get_device_test_data(db, device)


@router.put("/{device_id}/test_data", response_model=TestDataRead)
async def replace_test_data(
    device_id: uuid.UUID,
    payload: TestDataPayload,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await device_services.test_data.replace_device_test_data(db, device, payload.root)


@router.patch("/{device_id}/test_data", response_model=TestDataRead)
async def merge_test_data(
    device_id: uuid.UUID,
    payload: TestDataPayload,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await device_services.test_data.merge_device_test_data(db, device, payload.root)


@router.get("/{device_id}/test_data/history", response_model=list[TestDataAuditEntryRead])
async def get_history(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    await get_device_or_404(device_id, db)
    logs = await device_services.test_data.get_test_data_history(db, device_id, limit=limit)
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
