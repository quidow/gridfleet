"""Test durable-job worker row factory."""

from __future__ import annotations

from datetime import timedelta

from app.seeding.factories.job import make_job
from tests.seeding.helpers import build_test_seed_context


def test_make_job_queued_has_no_started_at() -> None:
    ctx = build_test_seed_context(seed=1)
    job = make_job(
        ctx,
        kind="device.restart",
        status="pending",
        scheduled_at=ctx.now,
    )
    assert job.status == "pending"
    assert job.started_at is None
    assert job.completed_at is None


def test_make_job_succeeded_sets_completed_at() -> None:
    ctx = build_test_seed_context(seed=1)
    job = make_job(
        ctx,
        kind="device.restart",
        status="completed",
        scheduled_at=ctx.now - timedelta(minutes=10),
        duration_seconds=45.0,
        attempts=1,
    )
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.completed_at is not None


def test_make_job_failed_respects_max_attempts() -> None:
    ctx = build_test_seed_context(seed=1)
    job = make_job(
        ctx,
        kind="device.restart",
        status="failed",
        scheduled_at=ctx.now - timedelta(hours=1),
        duration_seconds=120.0,
        attempts=3,
        max_attempts=3,
    )
    assert job.attempts == job.max_attempts
