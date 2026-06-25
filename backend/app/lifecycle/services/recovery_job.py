"""Durable-job kind that runs a one-shot ``attempt_auto_recovery``."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from app.jobs.models import Job

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.policy import LifecyclePolicyService

logger = get_logger(__name__)


class RecoveryJobService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        settings: SettingsReader,
        lifecycle_policy: LifecyclePolicyService,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._lifecycle_policy = lifecycle_policy

    @staticmethod
    async def _finalize_job_row(
        db: AsyncSession,
        row: Job,
        *,
        status: str,
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        """Stamp ``row`` with a terminal ``status`` plus snapshot mutations and commit."""
        row.status = status
        snapshot = copy.deepcopy(row.snapshot)
        snapshot["status"] = status
        if note is not None:
            snapshot["note"] = note
        if error is not None:
            snapshot["error"] = error
        snapshot["finished_at"] = now_utc().isoformat()
        row.snapshot = snapshot
        row.completed_at = now_utc()
        await db.commit()

    async def _lock_and_recover(
        self,
        db: AsyncSession,
        parsed_job_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        source: str,
        reason: str,
    ) -> None:
        """Lock the device, run recovery, and finalize the job row inside ``db``."""
        row = await db.get(Job, parsed_job_id)
        try:
            device = await device_locking.lock_device(db, device_id)
        except NoResultFound:
            logger.info(
                "device_recovery: device %s no longer exists; marking job complete",
                device_id,
            )
            if row is not None:
                await self._finalize_job_row(db, row, status=JOB_STATUS_COMPLETED, note="Device no longer exists")
            return
        except Exception:
            logger.exception("device_recovery: failed to lock device %s", device_id)
            if row is not None:
                await self._finalize_job_row(
                    db, row, status=JOB_STATUS_FAILED, error=f"Device {device_id} could not be locked"
                )
            return

        await self._lifecycle_policy.attempt_auto_recovery(db, device, source=source, reason=reason)

        # Re-load the job row in this session since attempt_auto_recovery
        # commits multiple times internally, expiring the row.
        row = await db.get(Job, parsed_job_id)
        if row is not None:
            await self._finalize_job_row(db, row, status=JOB_STATUS_COMPLETED)

    async def run_device_recovery_job(self, job_id: str, payload: dict[str, Any]) -> None:
        """Run ``attempt_auto_recovery`` for the device named in ``payload``."""
        parsed_job_id = uuid.UUID(job_id)
        source = payload.get("source", "exit_maintenance")
        reason = payload.get("reason", "Operator exited maintenance")

        try:
            device_id = uuid.UUID(str(payload["device_id"]))
            async with self._session_factory() as db:
                await self._lock_and_recover(db, parsed_job_id, device_id, source=source, reason=reason)
        except Exception:
            logger.exception("device_recovery: job %s for device %s crashed", job_id, payload.get("device_id"))
            async with self._session_factory() as db:
                row = await db.get(Job, parsed_job_id)
                if row is None:
                    return
                await self._finalize_job_row(
                    db, row, status=JOB_STATUS_FAILED, error="device_recovery job crashed unexpectedly"
                )
