from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from app.services.host_tools_execution import (
    HOST_TOOLS_ENSURE_NAMESPACE,
    _ensure_host_tools_versions,
    configured_tool_versions,
    store_tool_ensure_result,
    utcnow,
)
from app.services.host_tools_runner import run_persisted_host_tool_ensure_job
from app.services.job_kind_constants import JOB_KIND_HOST_TOOLS_ENSURE
from app.services.job_queue import create_job, get_job
from app.services.job_status_constants import JOB_STATUS_PENDING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

__all__ = [
    "HOST_TOOLS_ENSURE_NAMESPACE",
    "configured_tool_versions",
    "run_persisted_host_tool_ensure_job",
    "store_tool_ensure_result",
    "utcnow",
]


def _job_payload(host: Host) -> dict[str, Any]:
    appium_version, selenium_version = configured_tool_versions()
    return {
        "host_id": str(host.id),
        "appium_version": appium_version,
        "selenium_jar_version": selenium_version,
    }


def _job_snapshot(
    *,
    job_id: uuid.UUID,
    host: Host,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    appium_version, selenium_version = configured_tool_versions()
    return {
        "job_id": str(job_id),
        "status": status,
        "host_id": str(host.id),
        "hostname": host.hostname,
        "target_versions": {
            "appium": appium_version,
            "selenium_jar": selenium_version,
        },
        "result": result,
        "error": error,
        "started_at": started_at or utcnow().isoformat(),
        "finished_at": finished_at,
    }


async def ensure_host_tools(db: AsyncSession, host: Host) -> dict[str, Any] | None:
    appium_version, selenium_version = configured_tool_versions()
    return await _ensure_host_tools_versions(
        db,
        host,
        appium_version=appium_version,
        selenium_version=selenium_version,
    )


async def start_host_tool_ensure_job(db: AsyncSession, host: Host) -> dict[str, Any]:
    job_id = uuid.uuid4()
    snapshot = _job_snapshot(job_id=job_id, host=host, status=JOB_STATUS_PENDING)
    row = await create_job(
        db,
        kind=JOB_KIND_HOST_TOOLS_ENSURE,
        payload=_job_payload(host),
        snapshot=snapshot,
        job_id=job_id,
    )
    return copy.deepcopy(row.snapshot)


async def get_host_tool_ensure_job(db: AsyncSession, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = await get_job(db, job_id)
    if row is None or row.kind != JOB_KIND_HOST_TOOLS_ENSURE:
        return None
    return copy.deepcopy(row.snapshot)
