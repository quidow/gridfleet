import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler_agent as node_manager
from app.appium_nodes.services.reconciler import converge_device_now
from app.core.dependencies import DbDep
from app.core.observability import get_logger
from app.devices.models import Device, DeviceHold
from app.devices.routers.helpers import get_device_for_update_or_404
from app.devices.schemas.device import AppiumNodeRead
from app.devices.services.readiness import assess_device_async, is_ready_for_use_async, readiness_error_detail_async
from app.runs import service as run_service

router = APIRouter(prefix="/api/devices", tags=["nodes"])
logger = get_logger(__name__)


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
async def start_node(device_id: uuid.UUID, db: DbDep) -> AppiumNode:
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
    try:
        return await node_manager.start_node(db, device, caller="operator_route")
    except (node_manager.NodeManagerError, node_manager.NodePortConflictError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{device_id}/node/stop", response_model=AppiumNodeRead)
async def stop_node(device_id: uuid.UUID, db: DbDep) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        raise HTTPException(status_code=400, detail=f"No running node for device {device.id}")
    try:
        return await node_manager.stop_node(db, device, caller="operator_route")
    except node_manager.NodeManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{device_id}/node/restart", response_model=AppiumNodeRead)
async def restart_node(device_id: uuid.UUID, db: DbDep) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="restart a node")
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        return await start_node(device_id, db)
    node = await node_manager.restart_node(db, device, caller="operator_restart")
    try:
        converged_node = await converge_device_now(device.id, db=db)
        if converged_node is not None:
            node = converged_node
    except Exception:  # noqa: BLE001 — best-effort convergence; route must return the restart node even if convergence fails
        logger.warning("operator_restart_immediate_convergence_failed", exc_info=True, device_id=str(device.id))
    await db.refresh(node)
    return node
