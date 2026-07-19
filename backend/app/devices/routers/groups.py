from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.http_errors import found_or_404
from app.devices.dependencies import DeviceServicesDep
from app.devices.group_keys import GroupKey
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
from app.devices.services import health as device_health
from app.devices.services import platform_label as platform_label_service
from app.devices.services.groups import (
    GroupKeyConflictError,
    GroupReferencedError,
    UnknownMemberOfError,
)
from app.lifecycle.services import remediation_log
from app.runs import service as run_service

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_GROUP_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/device-groups", tags=["device-groups"], responses=DEVICE_GROUP_ERROR_RESPONSES)


async def _group_device_ids_or_404(
    db: AsyncSession, group_key: GroupKey, device_services: DeviceServicesDep
) -> list[UUID]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")
    return [device.id for device in group["devices"]]


@router.post("", response_model=DeviceGroupRead, response_model_exclude_none=True, status_code=201)
async def create_group(data: DeviceGroupCreate, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    try:
        group = await device_services.groups.create_group(db, data)
    except GroupKeyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownMemberOfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await device_services.groups.get_group(db, group.key) or {}


@router.get("", response_model=list[DeviceGroupRead], response_model_exclude_none=True)
async def list_groups(db: DbDep, device_services: DeviceServicesDep) -> list[dict[str, Any]]:
    return await device_services.groups.list_groups(db)


@router.get("/{group_key}", response_model=DeviceGroupDetail, response_model_exclude_none=True)
async def get_group(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    group = found_or_404(await device_services.groups.get_group(db, group_key), "Group not found")

    devices = list(group.get("devices", []))
    payload = dict(group)
    if not devices:
        payload["devices"] = []
        return payload

    # Mirror the device-list batching: load reservation map, remediation
    # ladders, platform labels, and presenter contexts once, then reuse them
    # for every serialize_device call so per-member queries do not return.
    device_ids = [device.id for device in devices]
    # Eager-load appium_node for every device so build_public_summary and
    # the presenter can read it synchronously without per-device IO.
    serialization_contexts = await device_services.presenter.build_serialization_contexts(db, devices)
    reservation_map = await run_service.get_device_reservation_map(db, device_ids)
    ladders = await remediation_log.load_ladders(db, device_ids)
    health_summary_map = {
        str(device.id): device_health.build_public_summary(
            device,
            policy_view=remediation_log.build_policy_view(ladders[device.id], device.lifecycle_policy_state),
        )
        for device in devices
    }
    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in devices),
    )
    serialized: list[dict[str, Any]] = []
    for device in devices:
        reservation_context = run_service.get_reservation_context_for_device(reservation_map.get(device.id), device.id)
        serialized.append(
            await device_services.presenter.serialize_device(
                db,
                device,
                reservation_context=reservation_context,
                health_summary=health_summary_map.get(str(device.id)),
                platform_label=label_map.get((device.pack_id, device.platform_id)),
                precomputed=serialization_contexts[device.id],
            )
        )
    payload["devices"] = serialized
    return payload


@router.patch("/{group_key}", response_model=DeviceGroupRead, response_model_exclude_none=True)
async def update_group(
    group_key: GroupKey,
    data: DeviceGroupUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    try:
        group = found_or_404(await device_services.groups.update_group(db, group_key, data), "Group not found")
    except UnknownMemberOfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await device_services.groups.get_group(db, group.key) or {}


@router.delete("/{group_key}", status_code=204)
async def delete_group(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> None:
    try:
        deleted = await device_services.groups.delete_group(db, group_key)
    except GroupReferencedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")


@router.post("/{group_key}/members")
async def add_members(
    group_key: GroupKey,
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
    group_key: GroupKey,
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
async def group_bulk_start(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_start_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/stop-nodes", response_model=BulkOperationResult)
async def group_bulk_stop(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_stop_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/restart-nodes", response_model=BulkOperationResult)
async def group_bulk_restart(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_restart_nodes(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/enter-maintenance", response_model=BulkOperationResult)
async def group_bulk_enter_maintenance(
    group_key: GroupKey,
    body: BulkDeviceIds,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_enter_maintenance(db, device_ids)


@router.post("/{group_key}/bulk/exit-maintenance", response_model=BulkOperationResult)
async def group_bulk_exit_maintenance(
    group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_exit_maintenance(db, device_ids)


@router.post("/{group_key}/bulk/reconnect", response_model=BulkOperationResult)
async def group_bulk_reconnect(
    group_key: GroupKey,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_reconnect(db, device_ids, caller="group")


@router.post("/{group_key}/bulk/update-tags", response_model=BulkOperationResult)
async def group_bulk_update_tags(
    group_key: GroupKey,
    body: BulkTagsUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_update_tags(db, device_ids, body.tags, body.merge)


@router.post("/{group_key}/bulk/delete", response_model=BulkOperationResult)
async def group_bulk_delete(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_delete(db, device_ids)
