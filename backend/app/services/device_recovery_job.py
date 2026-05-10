"""Durable-job kind that runs a one-shot ``attempt_auto_recovery``."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.models.job import Job
from app.observability import get_logger
from app.services import device_locking, lifecycle_policy
from app.services.job_status_constants import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_device_recovery_job(
    job_id: str,
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run ``attempt_auto_recovery`` for the device named in ``payload``."""
    parsed_job_id = uuid.UUID(job_id)
    device_id = uuid.UUID(str(payload["device_id"]))
    source = payload.get("source", "exit_maintenance")
    reason = payload.get("reason", "Operator exited maintenance")

    try:
        async with session_factory() as db:
            row = await db.get(Job, parsed_job_id)
            try:
                device = await device_locking.lock_device(db, device_id)
            except Exception:
                logger.exception("device_recovery: device %s missing or lock failed", device_id)
                if row is not None:
                    row.status = JOB_STATUS_FAILED
                    snapshot = copy.deepcopy(row.snapshot)
                    snapshot["status"] = JOB_STATUS_FAILED
                    snapshot["error"] = f"Device {device_id} not found or could not be locked"
                    snapshot["finished_at"] = _utcnow().isoformat()
                    row.snapshot = snapshot
                    row.completed_at = _utcnow()
                    await db.commit()
                return

            await lifecycle_policy.attempt_auto_recovery(db, device, source=source, reason=reason)

            # Re-load the job row in this session since attempt_auto_recovery
            # commits multiple times internally, expiring the row.
            row = await db.get(Job, parsed_job_id)
            if row is not None:
                row.status = JOB_STATUS_COMPLETED
                snapshot = copy.deepcopy(row.snapshot)
                snapshot["status"] = JOB_STATUS_COMPLETED
                snapshot["finished_at"] = _utcnow().isoformat()
                row.snapshot = snapshot
                row.completed_at = _utcnow()
                await db.commit()
    except Exception:
        logger.exception("device_recovery: job %s for device %s crashed", job_id, device_id)
        async with session_factory() as db:
            row = await db.get(Job, parsed_job_id)
            if row is None:
                return
            row.status = JOB_STATUS_FAILED
            snapshot = copy.deepcopy(row.snapshot)
            snapshot["status"] = JOB_STATUS_FAILED
            snapshot["error"] = "device_recovery job crashed unexpectedly"
            snapshot["finished_at"] = _utcnow().isoformat()
            row.snapshot = snapshot
            row.completed_at = _utcnow()
            await db.commit()
