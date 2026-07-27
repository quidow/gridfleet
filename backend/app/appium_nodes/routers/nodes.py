from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.dependencies import AppiumNodeServicesDep
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler_agent as node_manager
from app.core.observability import get_logger
from app.devices import locking as device_locking
from app.devices.schemas.device import AppiumNodeRead
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.readiness import assess_device_async, is_ready_for_use_async, readiness_error_detail_async
from app.runs import service as run_service

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.services_container import AppiumNodeServices
    from app.devices.locking import LockedDevice
    from app.devices.models import Device

    type StartableEffect = Callable[
        [AsyncSession, LockedDevice, AppiumNodeServices], Awaitable[tuple[AppiumNode, bool]]
    ]

router = APIRouter(prefix="/api/devices", tags=["nodes"])
logger = get_logger(__name__)


async def _lock_device_or_404(db: AsyncSession, device_id: uuid.UUID) -> LockedDevice:
    """Take the only Device lock these routes hold, on the command's own session.

    There is no pre-lock on the request session: the command locks the same row
    from a second session, and holding both would deadlock until a statement
    timeout.
    """
    try:
        return await device_locking.lock_device_handle(db, device_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="Device not found") from exc


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


# The two fallbacks below used to re-enter each other's *route*, which re-ran that
# route's guards and — decisively — carried that route's own ``caller``. ``caller``
# reaches the operator intent and the desired-state reason, so the pair is
# deliberately asymmetric: start→restart keeps ``operator_restart`` while
# restart→start downgrades to ``operator_route``. Folding them onto one caller
# value would silently rewrite operator intent reasons.
#
# Each fallback lands in the other function's non-fallback branch — the node row is
# not re-read and nothing mutates in between — so the mutual recursion resolves in
# exactly one hop. The re-entered route's duplicate reserved/maintenance/verified
# guards are gone: inside one transaction they re-read the row the first pass
# already accepted.


async def _start_effect(
    db: AsyncSession, locked: LockedDevice, appium_services: AppiumNodeServices
) -> tuple[AppiumNode, bool]:
    """Apply the start lever. Returns the node row and whether to poke the agent."""
    device = locked.device
    node: AppiumNode | None = device.appium_node
    if node is not None and node.desired_state == AppiumDesiredState.running:
        if node.observed_running:
            raise HTTPException(status_code=409, detail=f"Node already running for device {device.id}")
        # Node is desired-running but down (e.g. after a crash). Plain start would
        # be a dead lever — request_start no-ops on an unchanged intent — so recover
        # via the restart path, which re-spawns and kicks an immediate convergence.
        return await _restart_effect(db, locked, appium_services)
    if not await is_ready_for_use_async(db, device):
        raise HTTPException(
            status_code=400,
            detail=await readiness_error_detail_async(db, device, action="start a node"),
        )
    if device.host_id is None:
        raise HTTPException(status_code=400, detail=f"Device {device.id} has no host assigned")
    return await appium_services.reconciler_agent.start_node_txn(db, locked, caller="operator_route"), False


async def _restart_effect(
    db: AsyncSession, locked: LockedDevice, appium_services: AppiumNodeServices
) -> tuple[AppiumNode, bool]:
    """Apply the restart lever. Returns the node row and whether to poke the agent."""
    node: AppiumNode | None = locked.device.appium_node
    if node is None or node.desired_state != AppiumDesiredState.running:
        return await _start_effect(db, locked, appium_services)
    return await appium_services.reconciler_agent.restart_node_txn(db, locked, caller="operator_restart"), True


async def _apply_startable_lever(
    device_id: uuid.UUID,
    appium_services: AppiumNodeServices,
    *,
    action: str,
    effect: StartableEffect,
) -> tuple[AppiumNodeRead, bool]:
    """Own the single transaction the start and restart levers share.

    The response snapshot is built before the transaction exits, so nothing reads
    an ORM row belonging to a closed session.
    """
    try:
        async with appium_services.session_factory.begin() as db:
            locked = await _lock_device_or_404(db, device_id)
            await _assert_device_not_reserved(locked.device, db)
            _assert_startable_outside_maintenance(locked.device)
            await _assert_device_verified(db, locked.device, action=action)
            node, poke = await effect(db, locked, appium_services)
            return AppiumNodeRead.model_validate(node, from_attributes=True), poke
    except (node_manager.NodeManagerError, node_manager.NodePortConflictError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _poke_agent(device_id: uuid.UUID, appium_services: AppiumNodeServices) -> None:
    try:
        # Best-effort wake hint, after the command transaction has closed:
        # converge_device_now opens and closes its own read session and writes
        # nothing, so the node snapshot taken above stays the source of truth.
        await appium_services.reconciler.converge_device_now(device_id)
    except Exception:  # noqa: BLE001 — best-effort convergence; route must return the restart node even if convergence fails
        logger.warning("operator_restart_immediate_convergence_failed", exc_info=True, device_id=str(device_id))


@router.post("/{device_id}/node/start", response_model=AppiumNodeRead)
async def start_node(device_id: uuid.UUID, appium_services: AppiumNodeServicesDep) -> AppiumNodeRead:
    node, poke = await _apply_startable_lever(device_id, appium_services, action="start a node", effect=_start_effect)
    if poke:
        await _poke_agent(device_id, appium_services)
    return node


@router.post("/{device_id}/node/stop", response_model=AppiumNodeRead)
async def stop_node(device_id: uuid.UUID, appium_services: AppiumNodeServicesDep) -> AppiumNodeRead:
    try:
        async with appium_services.session_factory.begin() as db:
            locked = await _lock_device_or_404(db, device_id)
            await _assert_device_not_reserved(locked.device, db)
            node: AppiumNode | None = locked.device.appium_node
            if node is None or node.desired_state != AppiumDesiredState.running:
                raise HTTPException(status_code=400, detail=f"No running node for device {locked.device.id}")
            stopped = await appium_services.reconciler_agent.stop_node_txn(db, locked, caller="operator_route")
            return AppiumNodeRead.model_validate(stopped, from_attributes=True)
    except node_manager.NodeManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{device_id}/node/restart", response_model=AppiumNodeRead)
async def restart_node(device_id: uuid.UUID, appium_services: AppiumNodeServicesDep) -> AppiumNodeRead:
    node, poke = await _apply_startable_lever(
        device_id, appium_services, action="restart a node", effect=_restart_effect
    )
    if poke:
        await _poke_agent(device_id, appium_services)
    return node
