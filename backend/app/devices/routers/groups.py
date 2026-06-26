from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.http_errors import found_or_404
from app.devices.dependencies import DeviceServicesDep
from app.devices.schemas.device import (
    BulkDeviceIds,
    BulkOperationResult,
    BulkTagsUpdate,
)
from app.devices.schemas.group import (
    DeviceGroupCreate,
    DeviceGroupDetail,
    DeviceGroupRead,
    DeviceGroupUpdate,
    GroupMembershipUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_GROUP_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/device-groups", tags=["device-groups"], responses=DEVICE_GROUP_ERROR_RESPONSES)


async def _group_device_ids_or_404(
    db: AsyncSession, group_id: uuid.UUID, device_services: DeviceServicesDep
) -> list[uuid.UUID]:
    device_ids = await device_services.groups.get_group_device_ids(db, group_id)
    if not device_ids:
        raise HTTPException(status_code=404, detail="Group not found or empty")
    return device_ids


@router.post("", response_model=DeviceGroupRead, response_model_exclude_none=True, status_code=201)
async def create_group(data: DeviceGroupCreate, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    group = await device_services.groups.create_group(db, data)
    return await device_services.groups.get_group(db, group.id) or {}


@router.get("", response_model=list[DeviceGroupRead], response_model_exclude_none=True)
async def list_groups(db: DbDep, device_services: DeviceServicesDep) -> list[dict[str, Any]]:
    return await device_services.groups.list_groups(db)


@router.get("/{group_id}", response_model=DeviceGroupDetail, response_model_exclude_none=True)
async def get_group(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    group = found_or_404(await device_services.groups.get_group(db, group_id), "Group not found")

    payload = dict(group)
    payload["devices"] = [
        await device_services.presenter.serialize_device(db, device) for device in group.get("devices", [])
    ]
    return payload


@router.patch("/{group_id}", response_model=DeviceGroupRead, response_model_exclude_none=True)
async def update_group(
    group_id: uuid.UUID,
    data: DeviceGroupUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    group = found_or_404(await device_services.groups.update_group(db, group_id, data), "Group not found")
    return await device_services.groups.get_group(db, group.id) or {}


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> None:
    deleted = await device_services.groups.delete_group(db, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")


@router.post("/{group_id}/members")
async def add_members(
    group_id: uuid.UUID,
    body: GroupMembershipUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, int]:
    group = found_or_404(await device_services.groups.get_group(db, group_id), "Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually add members to a dynamic group")
    added = await device_services.groups.add_members(db, group_id, body.device_ids)
    return {"added": added}


@router.delete("/{group_id}/members")
async def remove_members(
    group_id: uuid.UUID,
    body: GroupMembershipUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, int]:
    group = found_or_404(await device_services.groups.get_group(db, group_id), "Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually remove members from a dynamic group")
    removed = await device_services.groups.remove_members(db, group_id, body.device_ids)
    return {"removed": removed}


@router.post("/{group_id}/bulk/start-nodes", response_model=BulkOperationResult)
async def group_bulk_start(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_start_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/stop-nodes", response_model=BulkOperationResult)
async def group_bulk_stop(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_stop_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/restart-nodes", response_model=BulkOperationResult)
async def group_bulk_restart(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_restart_nodes(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/enter-maintenance", response_model=BulkOperationResult)
async def group_bulk_enter_maintenance(
    group_id: uuid.UUID,
    body: BulkDeviceIds,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_enter_maintenance(db, device_ids)


@router.post("/{group_id}/bulk/exit-maintenance", response_model=BulkOperationResult)
async def group_bulk_exit_maintenance(
    group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_exit_maintenance(db, device_ids)


@router.post("/{group_id}/bulk/reconnect", response_model=BulkOperationResult)
async def group_bulk_reconnect(
    group_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_reconnect(db, device_ids, caller="group")


@router.post("/{group_id}/bulk/update-tags", response_model=BulkOperationResult)
async def group_bulk_update_tags(
    group_id: uuid.UUID,
    body: BulkTagsUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_update_tags(db, device_ids, body.tags, body.merge)


@router.post("/{group_id}/bulk/delete", response_model=BulkOperationResult)
async def group_bulk_delete(group_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_id, device_services)
    return await device_services.bulk.bulk_delete(db, device_ids)
