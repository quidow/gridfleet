import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceHold
from app.devices.services.intent import register_intents_and_reconcile, revoke_intents_and_reconcile
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_MAINTENANCE,
    RECOVERY,
    DeviceHoldPrecondition,
    IntentRegistration,
)
from app.devices.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_maintenance_reason,
    clear_maintenance_recovery_suppression,
    set_maintenance_reason,
)
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_hooks import EventLogHook, IncidentHook, RunExclusionHook
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.devices.services.state import legacy_label_for_audit
from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)

_MACHINE = DeviceStateMachine(hooks=[EventLogHook(), IncidentHook(), RunExclusionHook()])


def _maintenance_sources(device_id: uuid.UUID) -> list[str]:
    return [
        f"maintenance:node:{device_id}",
        f"maintenance:grid:{device_id}",
        f"maintenance:recovery:{device_id}",
    ]


def _maintenance_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    precondition: DeviceHoldPrecondition = {
        "kind": "device_hold",
        "device_id": str(device_id),
        "hold": "maintenance",
    }
    return [
        IntentRegistration(
            source=f"maintenance:node:{device_id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_MAINTENANCE, "stop_mode": "graceful"},
            precondition=precondition,
        ),
        IntentRegistration(
            source=f"maintenance:grid:{device_id}",
            axis=GRID_ROUTING,
            payload={"accepting_new_sessions": False, "priority": PRIORITY_MAINTENANCE},
            precondition=precondition,
        ),
        IntentRegistration(
            source=f"maintenance:recovery:{device_id}",
            axis=RECOVERY,
            # Reason must match ``MAINTENANCE_HOLD_SUPPRESSION_REASON`` exactly:
            # ``clear_maintenance_recovery_suppression`` (called from
            # ``exit_maintenance``) only clears
            # ``lifecycle_policy_state.recovery_suppressed_reason`` when its
            # value equals that constant. Any drift here freezes the device's
            # node ``effective_state`` at "blocked" after an operator exit.
            payload={
                "allowed": False,
                "priority": PRIORITY_MAINTENANCE,
                "reason": MAINTENANCE_HOLD_SUPPRESSION_REASON,
            },
            precondition=precondition,
        ),
    ]


async def enter_maintenance(
    db: AsyncSession,
    device: Device,
    *,
    commit: bool = True,
    allow_reserved: bool = False,
    maintenance_reason: str = "Operator entered maintenance",
    publisher: EventPublisher | None = None,
) -> Device:
    if not allow_reserved and device.hold == DeviceHold.reserved:
        raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

    await _MACHINE.transition(
        device,
        TransitionEvent.MAINTENANCE_ENTERED,
        reason=maintenance_reason,
        publisher=publisher,
    )

    set_maintenance_reason(device, maintenance_reason)

    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=_maintenance_intents(device.id),
        reason=maintenance_reason,
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
    publisher: EventPublisher | None = None,
) -> Device:
    if device.hold != DeviceHold.maintenance:
        raise ValueError(f"Device is not in maintenance (status: {legacy_label_for_audit(device)})")

    await _MACHINE.transition(
        device,
        TransitionEvent.MAINTENANCE_EXITED,
        reason="Operator exited maintenance",
        publisher=publisher,
    )
    clear_maintenance_recovery_suppression(device)
    clear_maintenance_reason(device)
    # Maintenance exit is a sanctioned "give it another chance" signal —
    # clear the review-shelving flag so the recovery loop picks the device
    # back up.
    from app.devices.services.review import clear_review_required  # noqa: PLC0415

    await clear_review_required(
        db,
        device,
        reason="Operator exited maintenance",
        source="exit_maintenance",
    )

    await revoke_intents_and_reconcile(
        db,
        device_id=device.id,
        sources=_maintenance_sources(device.id),
        reason="Operator exited maintenance",
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
        except Exception:  # noqa: BLE001 — best-effort recovery scheduling; device_connectivity_loop is the fallback
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
    from app.jobs import JOB_KIND_DEVICE_RECOVERY, JOB_STATUS_PENDING  # noqa: PLC0415
    from app.jobs import queue as job_queue  # noqa: PLC0415

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
