from __future__ import annotations

import copy
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from app.core.timeutil import now_utc
from app.jobs.models import Job

if TYPE_CHECKING:
    from app.core.type_defs import SessionFactory
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher

VERIFICATION_EVENT = "device.verification.updated"
STAGE_NAMES = ("validation", "device_health", "node_start", "session_probe", "cleanup", "save_device")


def _verification_severity(job_status: str, stage_status: str | None) -> EventSeverity:
    """Derive event severity from verification job status.

    completed → success (device verified OK)
    failed with a stage that has status "failed" → warning (soft failure)
    failed with no stage info (hard error / exception path) → critical
    running/pending → info (progress update)
    """
    if job_status == "completed":
        return "success"
    if job_status == "failed":
        # A stage-level failure (e.g. session probe didn't pass) is a warning;
        # a hard failure with no stage info (exception before any stage ran) is critical.
        if stage_status == "failed":
            return "warning"
        return "critical"
    return "info"


_SESSION_FACTORY_KEY = "_session_factory"
_DB_JOB_ID_KEY = "_db_job_id"
_PUBLISHER_KEY = "_publisher"


def now_iso() -> str:
    return now_utc().isoformat()


def new_stage(name: str) -> dict[str, Any]:
    return {"name": name, "status": "pending", "detail": None}


def new_job(job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "operation_id": job_id,
        "status": "pending",
        "current_stage": None,
        "error": None,
        "device_id": None,
        "stages": [new_stage(name) for name in STAGE_NAMES],
        "started_at": now_iso(),
        "finished_at": None,
    }


def snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})


def reset_snapshot_for_retry(existing: dict[str, Any]) -> dict[str, Any]:
    reset = new_job(str(existing["job_id"]))
    reset["started_at"] = existing.get("started_at", reset["started_at"])
    # The operation token is immutable across retries — a re-run of the same Job
    # reuses the same ``operation_id`` so a crashed episode's tokenized lease is
    # resumed, never duplicated.
    reset["operation_id"] = existing.get("operation_id", reset["operation_id"])
    return reset


def hydrate_job(
    snapshot_data: dict[str, Any],
    *,
    db_job_id: str,
    payload: dict[str, Any],
    session_factory: SessionFactory,
    publisher: EventPublisher,
) -> dict[str, Any]:
    job = snapshot(snapshot_data)
    # The operation token is ``str(Job.id)``: the payload the request was
    # enqueued with, the persisted snapshot, and the database Job id must all
    # agree, or the job row was tampered with / built by a stale writer.
    snapshot_operation_id = str(job.get("operation_id"))
    payload_operation_id = str(payload.get("operation_id"))
    if snapshot_operation_id != db_job_id or payload_operation_id != db_job_id:
        raise ValueError(
            f"verification job {db_job_id} operation-id mismatch "
            f"(snapshot={snapshot_operation_id!r}, payload={payload_operation_id!r})"
        )
    job[_DB_JOB_ID_KEY] = db_job_id
    job[_SESSION_FACTORY_KEY] = session_factory
    job[_PUBLISHER_KEY] = publisher
    return job


def public_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    serialized = snapshot(job)
    current_stage_name, current_stage = _resolve_current_stage(serialized)
    return {
        "job_id": serialized["job_id"],
        "status": serialized["status"],
        "current_stage": current_stage_name,
        "current_stage_status": current_stage.get("status") if current_stage else None,
        "detail": current_stage.get("detail") if current_stage else None,
        "error": serialized.get("error"),
        "device_id": serialized.get("device_id"),
        "started_at": serialized["started_at"],
        "finished_at": serialized.get("finished_at"),
    }


async def publish(job: dict[str, Any]) -> None:
    await persist_job(job)
    snap = snapshot(job)
    job_status = str(snap.get("status", "pending"))
    # Determine current stage status for severity derivation.
    _, current_stage = _resolve_current_stage(snap)
    stage_status = current_stage.get("status") if current_stage else None
    publisher: EventPublisher = job[_PUBLISHER_KEY]
    await publisher.publish(
        VERIFICATION_EVENT,
        snap,
        severity=_verification_severity(job_status, stage_status),
    )


async def persist_job(job: dict[str, Any]) -> None:
    session_factory = cast("SessionFactory", job[_SESSION_FACTORY_KEY])
    async with session_factory() as db:
        row = await db.get(Job, job[_DB_JOB_ID_KEY])
        if row is None:
            return
        row.snapshot = snapshot(job)
        row.status = str(job["status"])
        finished_at = job.get("finished_at")
        row.completed_at = datetime.fromisoformat(finished_at) if isinstance(finished_at, str) else None
        await db.commit()


def stage(job: dict[str, Any], name: str) -> dict[str, Any]:
    for current_stage in cast("list[dict[str, Any]]", job["stages"]):
        if current_stage["name"] == name:
            return current_stage
    raise KeyError(name)


async def set_stage(
    job: dict[str, Any],
    name: str,
    status: str,
    *,
    detail: str | None = None,
) -> None:
    current_stage = stage(job, name)
    current_stage["status"] = status
    current_stage["detail"] = detail
    job["current_stage"] = name
    if job["status"] == "pending":
        job["status"] = "running"
    await publish(job)


async def finish_job(
    job: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    device_id: str | None = None,
) -> None:
    job["status"] = status
    job["error"] = error
    job["device_id"] = device_id
    job["finished_at"] = now_iso()
    await publish(job)


def _resolve_current_stage(job: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    stages = cast("list[dict[str, Any]]", job.get("stages") or [])
    job_status = job.get("status")

    if job_status == "failed":
        failed_stages = [current_stage for current_stage in stages if current_stage.get("status") == "failed"]
        if failed_stages:
            current_stage = failed_stages[-1]
            name = current_stage.get("name")
            return (str(name) if isinstance(name, str) else None), current_stage

    if job_status == "completed":
        for current_stage in stages:
            if current_stage.get("name") == "save_device" and current_stage.get("status") == "passed":
                return "save_device", current_stage

    stage_name = job.get("current_stage")
    if isinstance(stage_name, str):
        for current_stage in stages:
            if current_stage.get("name") == stage_name:
                return stage_name, current_stage

    for current_stage in stages:
        if current_stage.get("status") in {"running", "failed"}:
            name = current_stage.get("name")
            return (str(name) if isinstance(name, str) else None), current_stage

    completed_stages = [current_stage for current_stage in stages if current_stage.get("status") != "pending"]
    if completed_stages:
        current_stage = completed_stages[-1]
        name = current_stage.get("name")
        return (str(name) if isinstance(name, str) else None), current_stage

    return None, None
