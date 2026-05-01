import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.services.event_bus import event_bus
from app.services.node_manager import get_node_manager
from app.services.node_manager_types import NodeManagerError

logger = logging.getLogger(__name__)


async def enter_maintenance(
    db: AsyncSession,
    device: Device,
    *,
    drain: bool = False,
    commit: bool = True,
    allow_reserved: bool = False,
) -> Device:
    if not allow_reserved and device.availability_status == DeviceAvailabilityStatus.reserved:
        raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

    old_availability_status = device.availability_status.value
    device.availability_status = DeviceAvailabilityStatus.maintenance

    if not drain and device.appium_node and device.appium_node.state == NodeState.running:
        try:
            manager = get_node_manager(device)
            await manager.stop_node(db, device)
            # stop_node commits via mark_node_stopped, releasing our row lock.
            # Re-acquire the Device row before restoring maintenance.
            from app.services import device_locking

            device = await device_locking.lock_device(db, device.id)
            device.availability_status = DeviceAvailabilityStatus.maintenance
        except NodeManagerError as exc:
            logger.warning("Failed to stop node for %s during maintenance: %s", device.id, exc)

    await event_bus.publish(
        "device.availability_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_availability_status": old_availability_status,
            "new_availability_status": DeviceAvailabilityStatus.maintenance.value,
        },
    )
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
    if device.availability_status != DeviceAvailabilityStatus.maintenance:
        raise ValueError(f"Device is not in maintenance (status: {device.availability_status.value})")

    device.availability_status = DeviceAvailabilityStatus.offline
    await event_bus.publish(
        "device.availability_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_availability_status": DeviceAvailabilityStatus.maintenance.value,
            "new_availability_status": DeviceAvailabilityStatus.offline.value,
        },
    )
    if commit:
        await db.commit()
        await db.refresh(device)
    return device
