from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.http_errors import found_or_404
from app.core.timeutil import now_utc
from app.devices.dependencies import DeviceServicesDep
from app.devices.group_keys import GroupKey
from app.devices.models import GroupType
from app.devices.schemas.device import (
    BulkDeviceIds,
    BulkOperationResult,
)
from app.devices.schemas.group import (
    DeviceGroupCreate,
    DeviceGroupDetail,
    DeviceGroupMutationRead,
    DeviceGroupRead,
    DeviceGroupUpdate,
    GroupMembershipUpdate,
)
from app.devices.services.groups import (
    GroupKeyConflictError,
    GroupReferencedError,
    GroupWriteResult,
    StaticGroupFiltersError,
    UnknownMemberOfError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services_container import DeviceServices

DEVICE_GROUP_ERROR_RESPONSES = STANDARD_ERROR_RESPONSES

router = APIRouter(prefix="/api/device-groups", tags=["device-groups"], responses=DEVICE_GROUP_ERROR_RESPONSES)


async def _group_device_ids_or_404(
    db: AsyncSession, group_key: GroupKey, device_services: DeviceServicesDep
) -> list[UUID]:
    """The group's member ids, or 404 when the key is unknown.

    Two cheap keyed reads instead of ``get_group``: the bulk routes need ids,
    not serialized members, and for a dynamic group ``get_group`` would gather
    operational state, readiness and reservations for the devices in scope
    before discarding all of it. Existence is checked separately because an
    existing group with no members must still return 200 with a zero count,
    which an empty id list alone cannot distinguish from a missing group.
    """
    found_or_404(await device_services.groups.get_group_type(db, group_key), "Group not found")
    return await device_services.groups.get_group_device_ids(db, group_key)


@router.post("", response_model=DeviceGroupMutationRead, response_model_exclude_none=True, status_code=201)
async def create_group(data: DeviceGroupCreate, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    del db  # the command owns its own session; the count below opens a second one
    try:
        async with device_services.session_factory.begin() as command_db:
            created = await device_services.groups.create_group(command_db, data)
    except GroupKeyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (StaticGroupFiltersError, UnknownMemberOfError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _with_dynamic_count(device_services, created)


async def _with_dynamic_count(device_services: DeviceServices, written: GroupWriteResult) -> dict[str, Any]:
    """The mutation payload, with a dynamic group's live count folded in.

    A second, read-only session on purpose: the count is a fleet-wide evaluator
    read, and running it inside the write boundary would hold the definition row
    lock across all of it. A failed count degrades to ``None`` there and is
    dropped from the response by ``response_model_exclude_none``.
    """
    payload = dict(written.payload)
    if written.is_dynamic:
        async with device_services.session_factory() as count_db:
            payload["device_count"] = await device_services.groups.dynamic_device_count(
                count_db, group_id=written.group_id, group_key=written.group_key
            )
    return payload


@router.get("", response_model=list[DeviceGroupRead], response_model_exclude_none=True)
async def list_groups(db: DbDep, device_services: DeviceServicesDep) -> list[dict[str, Any]]:
    return await device_services.groups.list_groups(db)


@router.get("/{group_key}", response_model=DeviceGroupDetail, response_model_exclude_none=True)
async def get_group(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    # One bounded projection batch selects the members (for a dynamic group) and
    # feeds the synchronous DTO builder, so serialization adds no per-member query.
    detail = found_or_404(
        await device_services.groups.load_group_detail(db, group_key, now=now_utc()),
        "Group not found",
    )
    payload = dict(detail.payload)
    payload["devices"] = [
        device_services.presenter.serialize_projected_device(device, detail.projections[device.id])
        for device in detail.devices
    ]
    return payload


@router.patch("/{group_key}", response_model=DeviceGroupMutationRead, response_model_exclude_none=True)
async def update_group(
    group_key: GroupKey,
    data: DeviceGroupUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    try:
        return found_or_404(await device_services.groups.update_group(db, group_key, data), "Group not found")
    except (StaticGroupFiltersError, UnknownMemberOfError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    group_type = found_or_404(await device_services.groups.get_group_type(db, group_key), "Group not found")
    if group_type == GroupType.dynamic:
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
    group_type = found_or_404(await device_services.groups.get_group_type(db, group_key), "Group not found")
    if group_type == GroupType.dynamic:
        raise HTTPException(status_code=400, detail="Cannot manually remove members from a dynamic group")
    removed = found_or_404(
        await device_services.groups.remove_members(db, group_key, body.device_ids), "Group not found"
    )
    return {"removed": removed}


@router.post("/{group_key}/bulk/start-nodes", response_model=BulkOperationResult)
async def group_bulk_start(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_start_nodes(device_ids, caller="group")


@router.post("/{group_key}/bulk/stop-nodes", response_model=BulkOperationResult)
async def group_bulk_stop(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_stop_nodes(device_ids, caller="group")


@router.post("/{group_key}/bulk/restart-nodes", response_model=BulkOperationResult)
async def group_bulk_restart(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_restart_nodes(device_ids, caller="group")


@router.post("/{group_key}/bulk/enter-maintenance", response_model=BulkOperationResult)
async def group_bulk_enter_maintenance(
    group_key: GroupKey,
    body: BulkDeviceIds,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_enter_maintenance(device_ids)


@router.post("/{group_key}/bulk/exit-maintenance", response_model=BulkOperationResult)
async def group_bulk_exit_maintenance(
    group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_exit_maintenance(device_ids)


@router.post("/{group_key}/bulk/reconnect", response_model=BulkOperationResult)
async def group_bulk_reconnect(
    group_key: GroupKey,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_reconnect(device_ids)


@router.post("/{group_key}/bulk/delete", response_model=BulkOperationResult)
async def group_bulk_delete(group_key: GroupKey, db: DbDep, device_services: DeviceServicesDep) -> dict[str, Any]:
    device_ids = await _group_device_ids_or_404(db, group_key, device_services)
    return await device_services.bulk.bulk_delete(device_ids)
