import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.timeutil import now_utc
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    IntentRegistration,
    verification_intent_source,
)
from app.devices.services.lifecycle_policy_state import (
    clear_maintenance_reason,
    clear_maintenance_recovery_suppression,
    set_maintenance_reason,
    state,
)
from app.devices.services.reservation_query import device_is_reserved

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.devices.protocols import ReviewProtocol
    from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)


class MaintenanceService:
    def __init__(self, *, settings: SettingsReader, publisher: EventPublisher, review: ReviewProtocol) -> None:
        self._settings = settings
        # Publisher is needed so the reconciler's derived maintenance enter/exit
        # emits device.operational_state_changed (SSE).
        self._publisher = publisher
        self._review = review

    async def enter_maintenance(
        self,
        db: AsyncSession,
        device: Device,
        *,
        commit: bool = True,
        allow_reserved: bool = False,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> Device:
        if not allow_reserved and await device_is_reserved(db, device.id):
            raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

        set_maintenance_reason(device, maintenance_reason)

        # set_maintenance_reason is the fact write; the inline reconcile derives the
        # maintenance:node graceful stop and maintenance:recovery deny from it.
        await IntentService(db).reconcile_now(device.id, publisher=self._publisher)

        if commit:
            await db.commit()
            await db.refresh(device)
        return device

    async def exit_maintenance(self, db: AsyncSession, device: Device, *, commit: bool = True) -> Device:
        if state(device).get("maintenance_reason") is None:
            raise ValueError("Device is not in maintenance")

        clear_maintenance_recovery_suppression(device)
        clear_maintenance_reason(device)
        # Maintenance exit is a sanctioned "give it another chance" signal —
        # clear the review-shelving flag so the recovery loop picks the device
        # back up.
        await self._review.clear_review_required(
            db,
            device,
            reason="Operator exited maintenance",
            source="exit_maintenance",
        )

        # §14.4a: register a verification intent so the device starts re-verifying
        # immediately rather than waiting for the next device_connectivity_loop tick.
        # expires_at mirrors _register_verification_node_intent in verification_execution.py:
        # startup_timeout_sec + session_viability_timeout_sec + 60 s safety margin.
        startup_timeout = self._settings.get_int("appium.startup_timeout_sec")
        viability_timeout = self._settings.get_int("general.session_viability_timeout_sec")
        verify_intent_deadline = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=verification_intent_source(device.id),
                    axis=NODE_PROCESS,
                    payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY},
                    expires_at=verify_intent_deadline,
                )
            ],
            publisher=self._publisher,
        )
        # clear_maintenance_reason above is the fact write; the verification-intent
        # reconcile just above re-derives with no maintenance intents (reason cleared).

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
