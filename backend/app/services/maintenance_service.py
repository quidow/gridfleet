import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.services import device_locking, job_queue
from app.services.device_state import legacy_label_for_audit, set_hold, set_operational_state
from app.services.job_kind_constants import JOB_KIND_DEVICE_RECOVERY
from app.services.job_status_constants import JOB_STATUS_PENDING
from app.services.lifecycle_policy_state import clear_maintenance_recovery_suppression
from app.services.node_service import stop_node
from app.services.node_service_types import NodeManagerError

logger = logging.getLogger(__name__)


async def enter_maintenance(
    db: AsyncSession,
    device: Device,
    *,
    commit: bool = True,
    allow_reserved: bool = False,
) -> Device:
    if not allow_reserved and device.hold == DeviceHold.reserved:
        raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

    await set_hold(
        device,
        DeviceHold.maintenance,
        reason="Operator entered maintenance",
    )

    if device.appium_node and device.appium_node.state == NodeState.running:
        try:
            await stop_node(db, device)
            # stop_node commits via mark_node_stopped, releasing our row lock.
            # Re-acquire the Device row before restoring maintenance.
            device = await device_locking.lock_device(db, device.id)
            await set_hold(
                device,
                DeviceHold.maintenance,
                reason="Operator entered maintenance (re-asserted after node stop)",
            )
        except NodeManagerError as exc:
            logger.warning("Failed to stop node for %s during maintenance: %s", device.id, exc)

    if commit:
        await db.commit()
        await db.refresh(device)
    return device


async def exit_maintenance(
    db: AsyncSession,
    device: Device,
    *,
    commit: bool = True,
) -> Device:
    if device.hold != DeviceHold.maintenance:
        raise ValueError(f"Device is not in maintenance (status: {legacy_label_for_audit(device)})")

    await set_hold(device, None, reason="Operator exited maintenance")
    await set_operational_state(device, DeviceOperationalState.offline, reason="Operator exited maintenance")
    clear_maintenance_recovery_suppression(device)

    if commit:
        await db.commit()
        await db.refresh(device)

    # D3: enqueue one-shot recovery so the operator does not see an idle
    # offline device while waiting for the next connectivity loop tick.
    # create_job commits internally; placing it after the state commit ensures
    # the worker (running in a separate session) sees the post-exit state.
    await job_queue.create_job(
        db,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={
            "device_id": str(device.id),
            "source": "exit_maintenance",
            "reason": "Operator exited maintenance",
        },
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
    )

    return device
