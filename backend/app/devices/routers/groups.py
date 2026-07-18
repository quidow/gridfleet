from __future__ import annotations

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
from app.devices.services.groups import GroupKeyConflictError

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_GROUP_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/device-groups", tags=["device-groups"], responses=DEVICE_GROUP_ERROR_RESPONSES)


async def _group_device_ids_or_404(db: AsyncSession, group_key: str, device_services: DeviceServicesDep) -> list[UUID]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")
    return [device.id for device in group["devices"]]


@router.post("", response_model=DeviceGroupRead, response_model_exclude_none=True, status_code=201)
async def create_group(data: DeviceGroupCreate, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    try:
        group = await device_services.groups.create_group(db, data)
    except GroupKeyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await device_services.groups.get_group(db, group.key) or {}


@router.get("", response_model=list[DeviceGroupRead], response_model_exclude_none=True)
async def list_groups(db: DbDep, device_services: DeviceServicesDep) -> list[dict[str, Any]]:
    return await device_services.groups.list_groups(db)


@router.get("/{group_key}", response_model=DeviceGroupDetail, response_model_exclude_none=True)
async def get_group(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")

    payload = dict(group)
    payload["devices"] = [
        await device_services.presenter.serialize_device(db, device) for device in group.get("devices", [])
    ]
    return payload


@router.patch("/{group_key}", response_model=DeviceGroupRead, response_model_exclude_none=True)
async def update_group(
    group_key: str,
    data: DeviceGroupUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    group = found_or_404(await device_services.groups.update_group(db, group_key, data), "Group not found")
    return await device_services.groups.get_group(db, group.key) or {}


@router.delete("/{group_key}", status_code=204)
async def delete_group(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> None:
    deleted = await device_services.groups.delete_group(db, group_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")


@router.post("/{group_key}/members")
async def add_members(
    group_key: str,
    body: GroupMembershipUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, int]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually add members to a dynamic group")
    added = found_or_404(await device_services.groups.add_members(db, group_key, body.device_ids), "Group not found")
    return {"added": added}


@router.delete("/{group_key}/members")
async def remove_members(
    group_key: str,
    body: GroupMembershipUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, int]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")
    if group["group_type"] == "dynamic":
        raise HTTPException(status_code=400, detail="Cannot manually remove members from a dynamic group")
    removed = found_or_404(
        await device_services.groups.remove_members(db, group_key, body.device_ids), "Group not found"
    )
    return {"removed": removed}


@router.post("/{group_key}/bulk/start-nodes", response_model=BulkOperationResult)
async def group_bulk_start(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_start_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/stop-nodes", response_model=BulkOperationResult)
async def group_bulk_stop(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_stop_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/restart-nodes", response_model=BulkOperationResult)
async def group_bulk_restart(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_restart_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/enter-maintenance", response_model=BulkOperationResult)
async def group_bulk_enter_maintenance(
    group_key: str,
    body: BulkDeviceIds,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_enter_maintenance(db, device_ids)


@router.post("/{group_key}/bulk/exit-maintenance", response_model=BulkOperationResult)
async def group_bulk_exit_maintenance(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_exit_maintenance(db, device_ids)


@router.post("/{group_key}/bulk/reconnect", response_model=BulkOperationResult)
async def group_bulk_reconnect(
    group_key: str,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_reconnect(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/update-tags", response_model=BulkOperationResult)
async def group_bulk_update_tags(
    group_key: str,
    body: BulkTagsUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_update_tags(db, device_ids, body.tags, body.merge)


@router.post("/{group_key}/bulk/delete", response_model=BulkOperationResult)
async def group_bulk_delete(group_key: str, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_delete(db, device_ids)
