import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceHold
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_MAINTENANCE,
    RECOVERY,
    IntentRegistration,
    MaintenanceActivePrecondition,
    verification_intent_source,
)
from app.devices.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_maintenance_reason,
    clear_maintenance_recovery_suppression,
    set_maintenance_reason,
    state,
)
from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)


def _maintenance_sources(device_id: uuid.UUID) -> list[str]:
    return [
        f"maintenance:node:{device_id}",
        f"maintenance:grid:{device_id}",
        f"maintenance:recovery:{device_id}",
    ]


def _maintenance_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    precondition: MaintenanceActivePrecondition = {
        "kind": "maintenance_active",
        "device_id": str(device_id),
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


class MaintenanceService:
    def __init__(self, *, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def enter_maintenance(
        self,
        db: AsyncSession,
        device: Device,
        *,
        commit: bool = True,
        allow_reserved: bool = False,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> Device:
        if not allow_reserved and device.hold == DeviceHold.reserved:
            raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

        set_maintenance_reason(device, maintenance_reason)

        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=_maintenance_intents(device.id),
            reason=maintenance_reason,
        )

        if commit:
            await db.commit()
            await db.refresh(device)
        return device

    async def exit_maintenance(self, db: AsyncSession, device: Device, *, commit: bool = True) -> Device:
        if state(device).get("maintenance_reason") is None:
            raise ValueError(f"Device is not in maintenance (hold: {device.hold!r})")

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

        # §14.4a: register a verification intent so the device starts re-verifying
        # immediately rather than waiting for the next device_connectivity_loop tick.
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=verification_intent_source(device.id),
                    axis=NODE_PROCESS,
                    payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY},
                )
            ],
            reason="Operator exited maintenance",
        )

        await IntentService(db).revoke_intents_and_reconcile(
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
                await _schedule_device_recovery(db, device.id)
            except Exception:  # noqa: BLE001 — best-effort recovery scheduling; device_connectivity_loop is the fallback
                logger.warning(
                    "exit_maintenance: failed to enqueue recovery job for %s; "
                    "device_connectivity_loop will pick it up on the next tick",
                    device.id,
                    exc_info=True,
                )

        return device

    async def schedule_device_recovery(self, db: AsyncSession, device_id: uuid.UUID) -> None:
        await _schedule_device_recovery(db, device_id)


async def _schedule_device_recovery(db: AsyncSession, device_id: uuid.UUID) -> None:
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
