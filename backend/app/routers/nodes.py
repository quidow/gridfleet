import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import Device, DeviceHold
from app.routers.device_route_helpers import get_device_for_update_or_404
from app.schemas.device import AppiumNodeRead
from app.services import run_service
from app.services.appium_reconciler_allocation import candidate_ports
from app.services.desired_state_writer import write_desired_state
from app.services.device_readiness import assess_device_async, is_ready_for_use_async, readiness_error_detail_async
from app.services.settings_service import settings_service

router = APIRouter(prefix="/api/devices", tags=["nodes"])


async def _assert_device_not_reserved(device: Device, db: AsyncSession) -> None:
    reservation = await run_service.get_device_reservation(db, device.id)
    if reservation is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Device is reserved by run '{reservation.name}' ({reservation.id})",
        )


def _assert_startable_outside_maintenance(device: Device) -> None:
    if device.hold == DeviceHold.maintenance:
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
    if device.appium_node is not None and device.appium_node.desired_state == AppiumDesiredState.running:
        raise HTTPException(status_code=400, detail=f"Node already desired-running for device {device.id}")
    if not await is_ready_for_use_async(db, device):
        raise HTTPException(
            status_code=400,
            detail=await readiness_error_detail_async(db, device, action="start a node"),
        )
    if device.host_id is None:
        raise HTTPException(status_code=400, detail=f"Device {device.id} has no host assigned")
    desired_port = (await candidate_ports(db, host_id=device.host_id))[0]
    node: AppiumNode | None = device.appium_node
    if node is None:
        node = AppiumNode(
            device_id=device.id,
            port=desired_port,
            grid_url=settings_service.get("grid.hub_url"),
        )
        db.add(node)
        await db.flush()
        device.appium_node = node
    await write_desired_state(
        db,
        node=node,
        target=AppiumDesiredState.running,
        caller="operator_route",
        desired_port=desired_port,
    )
    await db.commit()
    await db.refresh(node)
    return node


@router.post("/{device_id}/node/stop", response_model=AppiumNodeRead)
async def stop_node(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        raise HTTPException(status_code=400, detail=f"No running node for device {device.id}")
    await write_desired_state(
        db,
        node=node,
        target=AppiumDesiredState.stopped,
        caller="operator_route",
    )
    await db.commit()
    await db.refresh(node)
    return node


@router.post("/{device_id}/node/restart", response_model=AppiumNodeRead)
async def restart_node(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="restart a node")
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        return await start_node(device_id, db)
    window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
    token = uuid.uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
    await write_desired_state(
        db,
        node=node,
        target=AppiumDesiredState.running,
        caller="operator_restart",
        desired_port=node.port,
        transition_token=token,
        transition_deadline=deadline,
    )
    await db.commit()
    await db.refresh(node)
    return node
