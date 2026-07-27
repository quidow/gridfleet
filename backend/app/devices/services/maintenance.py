import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceEventType
from app.devices.services.claims import device_is_reserved
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)
from app.devices.services.lifecycle_policy_state import (
    clear_maintenance_reason,
    set_maintenance_reason,
    state,
)
from app.lifecycle.services import remediation_log

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.devices.protocols import ReviewProtocol
    from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """A committed maintenance exit that still owes a durable recovery job.

    Carries only the device id: the enqueue runs on a session the maintenance
    transaction no longer owns, so nothing ORM-shaped may cross this boundary.
    """

    device_id: uuid.UUID


class MaintenanceService:
    def __init__(
        self,
        *,
        settings: SettingsReader,
        publisher: EventPublisher,
        review: ReviewProtocol,
        session_factory: SessionFactory,
    ) -> None:
        self._settings = settings
        # Publisher is needed so the reconciler's derived maintenance enter/exit
        # emits device.operational_state_changed (SSE).
        self._publisher = publisher
        self._review = review
        # Only ``schedule_device_recovery`` uses this: the durable enqueue owns its
        # own commit and therefore needs a session the caller's transaction has
        # already released.
        self._session_factory = session_factory

    async def enter_maintenance(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        allow_reserved: bool = False,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> None:
        """Acquire the Device aggregate lock once and enter maintenance under it.

        Transaction-local: the caller owns the boundary. Raises ``NoResultFound``
        when the device is gone and ``ValueError`` when it is reserved.
        """
        locked = await device_locking.lock_device_handle(db, device_id)
        await self.enter_maintenance_locked(
            db, locked, allow_reserved=allow_reserved, maintenance_reason=maintenance_reason
        )

    async def enter_maintenance_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        allow_reserved: bool = False,
        maintenance_reason: str = "Operator entered maintenance",
    ) -> None:
        """Fold one maintenance entry under the caller's Device lock. Flush only."""
        locked.assert_active(db)
        device = locked.device
        if not allow_reserved and await device_is_reserved(db, device.id):
            raise ValueError("Device is reserved by an active run; release the run before entering maintenance")

        entering = state(device).get("maintenance_reason") is None
        set_maintenance_reason(device, maintenance_reason)
        if entering:
            await record_event(db, device.id, DeviceEventType.maintenance_entered, {"reason": maintenance_reason})
            await remediation_log.append_reset(
                db,
                device.id,
                source="maintenance",
                action="maintenance_entered",
                reason=maintenance_reason,
            )

        # set_maintenance_reason is the fact write; the inline reconcile derives the
        # maintenance:node graceful stop and maintenance:recovery deny from it.
        await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
        await db.flush()

    async def exit_maintenance(self, db: AsyncSession, device_id: uuid.UUID) -> RecoveryRequest | None:
        """Acquire the Device aggregate lock once and leave maintenance under it.

        Transaction-local. The returned :class:`RecoveryRequest` is owed to
        ``schedule_device_recovery`` once the caller's transaction has committed.
        """
        locked = await device_locking.lock_device_handle(db, device_id)
        return await self.exit_maintenance_locked(db, locked)

    async def exit_maintenance_locked(self, db: AsyncSession, locked: LockedDevice) -> RecoveryRequest | None:
        """Fold one maintenance exit under the caller's Device lock. Flush only."""
        locked.assert_active(db)
        device = locked.device
        if state(device).get("maintenance_reason") is None:
            raise ValueError("Device is not in maintenance")

        clear_maintenance_reason(device)
        await record_event(db, device.id, DeviceEventType.maintenance_exited, {"reason": "exit maintenance"})
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
        # expires_at mirrors the verification lease deadline in preparation.py:
        # startup_timeout_sec + session_viability_timeout_sec + 60 s safety margin.
        startup_timeout = self._settings.get_int("appium.startup_timeout_sec")
        viability_timeout = self._settings.get_int("general.session_viability_timeout_sec")
        verify_intent_deadline = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=verification_intent_source(device.id),
                    kind=CommandKind.verification_start,
                    payload={"action": "start"},
                    expires_at=verify_intent_deadline,
                )
            ],
            publisher=self._publisher,
        )
        # clear_maintenance_reason above is the fact write; the verification-intent
        # reconcile just above re-derives with no maintenance intents (reason cleared).
        await db.flush()
        # D3: the operator should not watch an idle offline device until the next
        # device_connectivity_loop tick. The job row belongs to its own transaction,
        # so it cannot be staged here — the caller enqueues it once this transaction
        # has ended.
        return RecoveryRequest(device.id)

    async def schedule_device_recovery(self, device_id: uuid.UUID) -> None:
        """Enqueue the durable recovery job on a fresh short transaction.

        This must run *after* the maintenance transaction ended. Enqueue failure
        is swallowed: the state mutation already committed and surfacing it would
        hand the operator a 500 for a device that really did leave maintenance.
        The device_connectivity_loop remains the fallback path.
        """
        try:
            async with self._session_factory.begin() as recovery_db:
                await _schedule_device_recovery(recovery_db, device_id)
        except Exception:  # noqa: BLE001 — best-effort recovery scheduling; device_connectivity_loop is the fallback
            logger.warning(
                "exit_maintenance: failed to enqueue recovery job for %s; "
                "device_connectivity_loop will pick it up on the next tick",
                device_id,
                exc_info=True,
            )


async def _schedule_device_recovery(db: AsyncSession, device_id: uuid.UUID) -> None:
    """Enqueue a one-shot device_recovery job for the given device.

    Stages one row in the durable job queue; the caller's transaction commits
    it. Safe to call after the device-state mutations are already committed.

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
