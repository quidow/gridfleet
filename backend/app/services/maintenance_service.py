import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceHold
from app.services import appium_node_locking
from app.services.appium_reconciler_allocation import candidate_ports
from app.services.desired_state_writer import write_desired_state
from app.services.device_state import legacy_label_for_audit
from app.services.lifecycle_policy_state import clear_maintenance_recovery_suppression
from app.services.lifecycle_state_machine import DeviceStateMachine
from app.services.lifecycle_state_machine_hooks import EventLogHook, IncidentHook, RunExclusionHook
from app.services.lifecycle_state_machine_types import TransitionEvent

logger = logging.getLogger(__name__)

_MACHINE = DeviceStateMachine(hooks=[EventLogHook(), IncidentHook(), RunExclusionHook()])


async def enter_maintenance(
    db: AsyncSession,
    device: Device,
    *,
    commit: bool = True,
    allow_reserved: bool = False,
) -> Device:
    if not allow_reserved and device.hold == DeviceHold.reserved:
        raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

    await _MACHINE.transition(
        device,
        TransitionEvent.MAINTENANCE_ENTERED,
        reason="Operator entered maintenance",
    )

    if device.appium_node and device.appium_node.observed_running:
        node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
        if node is not None:
            await write_desired_state(
                db,
                node=node,
                target=NodeState.stopped,
                caller="maintenance_enter",
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
    if device.hold != DeviceHold.maintenance:
        raise ValueError(f"Device is not in maintenance (status: {legacy_label_for_audit(device)})")

    await _MACHINE.transition(
        device,
        TransitionEvent.MAINTENANCE_EXITED,
        reason="Operator exited maintenance",
    )
    clear_maintenance_recovery_suppression(device)

    node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    if node is not None:
        desired_port = None
        if device.host_id is not None:
            desired_port = (await candidate_ports(db, host_id=device.host_id))[0]
        await write_desired_state(
            db,
            node=node,
            target=NodeState.running,
            caller="maintenance_exit",
            desired_port=desired_port,
        )

    if commit:
        await db.commit()
        await db.refresh(device)
        # D3: schedule recovery so the operator does not see an idle offline
        # device while waiting for the next device_connectivity_loop tick.
        # Bulk callers pass commit=False and enqueue their own jobs after
        # their own final commit, to avoid create_job committing mid-loop.
        # Enqueue failure must not raise back to the operator after the
        # state mutation already committed — the device_connectivity_loop
        # remains the fallback path.
        try:
            await schedule_device_recovery(db, device.id)
        except Exception:
            logger.warning(
                "exit_maintenance: failed to enqueue recovery job for %s; "
                "device_connectivity_loop will pick it up on the next tick",
                device.id,
                exc_info=True,
            )

    return device


async def schedule_device_recovery(db: AsyncSession, device_id: uuid.UUID) -> None:
    """Enqueue a one-shot device_recovery job for the given device.

    Creates and commits one row in the durable job queue. Safe to call
    after the device-state mutations are already committed.

    Lazy import of job_queue + the job-kind/status constants breaks an
    import cycle (maintenance_service → job_queue → device_recovery_job →
    lifecycle_policy → maintenance_service) that CodeQL flags. The cycle
    is benign at runtime today but lazy import keeps the dependency graph
    clean and avoids future surprise on analyzer changes.
    """
    from app.services import job_queue  # noqa: PLC0415
    from app.services.job_kind_constants import JOB_KIND_DEVICE_RECOVERY  # noqa: PLC0415
    from app.services.job_status_constants import JOB_STATUS_PENDING  # noqa: PLC0415

    await job_queue.create_job(
        db,
        kind=JOB_KIND_DEVICE_RECOVERY,
        payload={
            "device_id": str(device_id),
            "source": "exit_maintenance",
            "reason": "Operator exited maintenance",
        },
        snapshot={"status": JOB_STATUS_PENDING},
        max_attempts=1,
    )
