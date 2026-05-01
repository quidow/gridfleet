import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.appium_node import AppiumNode
from app.models.device import Device, DeviceAvailabilityStatus
from app.routers.device_route_helpers import get_device_for_update_or_404
from app.schemas.device import AppiumNodeRead
from app.services import device_health_summary, run_service
from app.services.device_readiness import assess_device_async
from app.services.node_manager import get_node_manager
from app.services.node_manager_types import NodeManagerError

router = APIRouter(prefix="/api/devices", tags=["nodes"])


async def _assert_device_not_reserved(device: Device, db: AsyncSession) -> None:
    reservation = await run_service.get_device_reservation(db, device.id)
    if reservation is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Device is reserved by run '{reservation.name}' ({reservation.id})",
        )


def _assert_startable_outside_maintenance(device: Device) -> None:
    if device.availability_status == DeviceAvailabilityStatus.maintenance:
        raise HTTPException(status_code=409, detail="Device is in maintenance mode")


async def _assert_device_verified(db: AsyncSession, device: Device, *, action: str) -> None:
    readiness = await assess_device_async(db, device)
    if readiness.readiness_state == "verified":
        return
    if readiness.readiness_state == "setup_required":
        missing = ", ".join(readiness.missing_setup_fields)
        raise HTTPException(status_code=409, detail=f"Device cannot {action} until setup is complete ({missing})")
    raise HTTPException(status_code=409, detail=f"Device cannot {action} until verification succeeds")


@router.post("/{device_id}/node/start", response_model=AppiumNodeRead)
async def start_node(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="start a node")
    manager = get_node_manager(device)
    try:
        node = await manager.start_node(db, device)
    except NodeManagerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await device_health_summary.update_node_state(db, device, running=True, state=node.state.value)
    await db.commit()
    return node


@router.post("/{device_id}/node/stop", response_model=AppiumNodeRead)
async def stop_node(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    manager = get_node_manager(device)
    try:
        node = await manager.stop_node(db, device)
    except NodeManagerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await device_health_summary.update_node_state(db, device, running=False, state=node.state.value)
    await db.commit()
    return node


@router.post("/{device_id}/node/restart", response_model=AppiumNodeRead)
async def restart_node(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="restart a node")
    manager = get_node_manager(device)
    try:
        node = await manager.restart_node(db, device)
    except NodeManagerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await device_health_summary.update_node_state(
        db,
        device,
        running=node.state.value == "running",
        state=node.state.value,
    )
    await db.commit()
    return node
