"""Durable-job worker row factory."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.models.job import Job

if TYPE_CHECKING:
    from app.seeding.context import SeedContext


def make_job(
    ctx: SeedContext,
    *,
    kind: str,
    status: str,
    scheduled_at: datetime,
    duration_seconds: float | None = None,
    attempts: int = 0,
    max_attempts: int = 3,
    payload: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> Job:
    job = Job(
        kind=kind,
        status=status,
        payload=payload or {},
        snapshot=snapshot or {},
        attempts=attempts,
        max_attempts=max_attempts,
        scheduled_at=scheduled_at,
    )
    if status in {"running", "completed", "failed", "cancelled"}:
        job.started_at = scheduled_at
    if status in {"completed", "failed", "cancelled"} and duration_seconds is not None:
        job.completed_at = scheduled_at + timedelta(seconds=duration_seconds)
    return job
