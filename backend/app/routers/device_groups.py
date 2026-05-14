import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import DbDep
from app.schemas.device import (
    BulkMaintenanceEnter,
    BulkOperationResult,
    BulkTagsUpdate,
)
from app.schemas.device_group import (
    DeviceGroupCreate,
    DeviceGroupDetail,
    DeviceGroupRead,
    DeviceGroupUpdate,
    GroupMembershipUpdate,
)
from app.services import bulk_service, device_group_service, device_presenter

router = APIRouter(prefix="/api/device-groups", tags=["device-groups"])


async def _group_device_ids_or_404(db: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    device_ids = await device_group_service.get_group_device_ids(db, group_id)
    if not device_ids:
        raise HTTPException(status_code=404, detail="Group not found or empty")
    return device_ids


@router.post("", response_model=DeviceGroupRead, response_model_exclude_none=True, status_code=201)
async def create_group(data: DeviceGroupCreate, db: DbDep) -> dict[str, Any]:
    group = await device_group_service.create_group(db, data)
    return await device_group_service.get_group(db, group.id) or {}


@router.get("", response_model=list[DeviceGroupRead], response_model_exclude_none=True)
async def list_groups(db: DbDep) -> list[dict[str, Any]]:
    return await device_group_service.list_groups(db)


@router.get("/{group_id}", response_model=DeviceGroupDetail, response_model_exclude_none=True)
async def get_group(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    group = await device_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    payload = dict(group)
    payload["devices"] = [await device_presenter.serialize_device(db, device) for device in group.get("devices", [])]
    return payload


@router.patch("/{group_id}", response_model=DeviceGroupRead, response_model_exclude_none=True)
async def update_group(group_id: uuid.UUID, data: DeviceGroupUpdate, db: DbDep) -> dict[str, Any]:
    group = await device_group_service.update_group(db, group_id, data)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return await device_group_service.get_group(db, group.id) or {}


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, db: DbDep) -> None:
    deleted = await device_group_service.delete_group(db, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")


@router.post("/{group_id}/members")
async def add_members(group_id: uuid.UUID, body: GroupMembershipUpdate, db: DbDep) -> dict[str, int]:
    group = await device_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually add members to a dynamic group")
    added = await device_group_service.add_members(db, group_id, body.device_ids)
    return {"added": added}


@router.delete("/{group_id}/members")
async def remove_members(group_id: uuid.UUID, body: GroupMembershipUpdate, db: DbDep) -> dict[str, int]:
    group = await device_group_service.get_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually remove members from a dynamic group")
    removed = await device_group_service.remove_members(db, group_id, body.device_ids)
    return {"removed": removed}


@router.post("/{group_id}/bulk/start-nodes", response_model=BulkOperationResult)
async def group_bulk_start(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_start_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/stop-nodes", response_model=BulkOperationResult)
async def group_bulk_stop(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_stop_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/restart-nodes", response_model=BulkOperationResult)
async def group_bulk_restart(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_restart_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/enter-maintenance", response_model=BulkOperationResult)
async def group_bulk_enter_maintenance(group_id: uuid.UUID, body: BulkMaintenanceEnter, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_enter_maintenance(db, device_ids)


@router.post("/{group_id}/bulk/exit-maintenance", response_model=BulkOperationResult)
async def group_bulk_exit_maintenance(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_exit_maintenance(db, device_ids)


@router.post("/{group_id}/bulk/reconnect", response_model=BulkOperationResult)
async def group_bulk_reconnect(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_reconnect(db, device_ids)


@router.post("/{group_id}/bulk/update-tags", response_model=BulkOperationResult)
async def group_bulk_update_tags(
    group_id: uuid.UUID,
    body: BulkTagsUpdate,
    db: DbDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_update_tags(db, device_ids, body.tags, body.merge)


@router.post("/{group_id}/bulk/delete", response_model=BulkOperationResult)
async def group_bulk_delete(group_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id)
    return await bulk_service.bulk_delete(db, device_ids)
