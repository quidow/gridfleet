import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.dependencies import AppiumNodeServicesDep
from app.appium_nodes.exceptions import NodeAlreadyRunningError, NodeStopNotAcknowledgedError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler_agent as node_manager
from app.core.dependencies import DbDep
from app.core.observability import get_logger
from app.devices.models import Device
from app.devices.routers.helpers import get_device_for_update_or_404
from app.devices.schemas.device import AppiumNodeRead
from app.devices.services.lifecycle_policy_state import in_maintenance
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
    if in_maintenance(device):
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
async def start_node(device_id: uuid.UUID, db: DbDep, appium_services: AppiumNodeServicesDep) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="start a node")
    if device.appium_node is not None and device.appium_node.desired_state == AppiumDesiredState.running:
        if device.appium_node.observed_running:
            raise HTTPException(status_code=409, detail=f"Node already running for device {device.id}")
        # Node is desired-running but down (e.g. after a crash). Plain start would
        # be a dead lever — request_start no-ops on an unchanged intent — so recover
        # via the restart path, which re-spawns and kicks an immediate convergence.
        return await restart_node(device_id, db, appium_services)
    if not await is_ready_for_use_async(db, device):
        raise HTTPException(
            status_code=400,
            detail=await readiness_error_detail_async(db, device, action="start a node"),
        )
    if device.host_id is None:
        raise HTTPException(status_code=400, detail=f"Device {device.id} has no host assigned")
    try:
        return await appium_services.reconciler_agent.start_node(db, device, caller="operator_route")
    except (node_manager.NodeManagerError, node_manager.NodePortConflictError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{device_id}/node/stop", response_model=AppiumNodeRead)
async def stop_node(device_id: uuid.UUID, db: DbDep, appium_services: AppiumNodeServicesDep) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        raise HTTPException(status_code=400, detail=f"No running node for device {device.id}")
    try:
        return await appium_services.reconciler_agent.stop_node(db, device, caller="operator_route")
    except node_manager.NodeManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{device_id}/node/restart", response_model=AppiumNodeRead)
async def restart_node(device_id: uuid.UUID, db: DbDep, appium_services: AppiumNodeServicesDep) -> AppiumNode:
    device = await get_device_for_update_or_404(device_id, db)
    await _assert_device_not_reserved(device, db)
    _assert_startable_outside_maintenance(device)
    await _assert_device_verified(db, device, action="restart a node")
    node: AppiumNode | None = device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        return await start_node(device_id, db, appium_services)
    node = await appium_services.reconciler_agent.restart_node(db, device, caller="operator_restart")
    try:
        converged_node = await appium_services.reconciler.converge_device_now(device.id, db=db)
        if converged_node is not None:
            node = converged_node
    except (NodeAlreadyRunningError, NodeStopNotAcknowledgedError):
        # Expected, self-healing transient in the relay re-register window — the
        # reconciler tick converges. Debug, not warning.
        logger.debug("operator_restart_immediate_convergence_transient", exc_info=True, device_id=str(device.id))
    except Exception:  # noqa: BLE001 — best-effort convergence; route must return the restart node even if convergence fails
        logger.warning("operator_restart_immediate_convergence_failed", exc_info=True, device_id=str(device.id))
    await db.refresh(node)
    return node
