from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from app.errors import AgentCallError
from app.models.job import Job
from app.services.host_service import get_host
from app.services.host_tools_execution import _ensure_host_tools_versions, store_tool_ensure_result, utcnow
from app.services.job_status_constants import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_RUNNING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def run_persisted_host_tool_ensure_job(
    job_id: str,
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    parsed_job_id = uuid.UUID(job_id)
    host_id = uuid.UUID(str(payload["host_id"]))
    async with session_factory() as db:
        row = await db.get(Job, parsed_job_id)
        host = await get_host(db, host_id)
        if row is None or host is None:
            return
        started_at = utcnow().isoformat()
        row.status = JOB_STATUS_RUNNING
        snapshot = copy.deepcopy(row.snapshot)
        snapshot["status"] = JOB_STATUS_RUNNING
        snapshot["started_at"] = started_at
        snapshot["error"] = None
        row.snapshot = snapshot
        await db.commit()

    try:
        async with session_factory() as db:
            host = await get_host(db, host_id)
            row = await db.get(Job, parsed_job_id)
            if host is None or row is None:
                return
            appium_version = payload.get("appium_version")
            selenium_version = payload.get("selenium_jar_version")
            result = (
                await _ensure_host_tools_versions(
                    db,
                    host,
                    appium_version=appium_version if isinstance(appium_version, str) else None,
                    selenium_version=selenium_version if isinstance(selenium_version, str) else None,
                )
                or {}
            )
            finished_at = utcnow().isoformat()
            row.status = JOB_STATUS_COMPLETED
            snapshot = copy.deepcopy(row.snapshot)
            snapshot["status"] = JOB_STATUS_COMPLETED
            snapshot["result"] = result
            snapshot["finished_at"] = finished_at
            row.snapshot = snapshot
            row.completed_at = utcnow()
            await db.commit()
    except (AgentCallError, OSError, ValueError) as exc:
        async with session_factory() as db:
            row = await db.get(Job, parsed_job_id)
            if row is None:
                return
            row.status = JOB_STATUS_FAILED
            snapshot = copy.deepcopy(row.snapshot)
            snapshot["status"] = JOB_STATUS_FAILED
            snapshot["error"] = str(exc)
            snapshot["finished_at"] = utcnow().isoformat()
            row.snapshot = snapshot
            row.completed_at = utcnow()
            await store_tool_ensure_result(db, host_id, {"error": str(exc)})
            await db.commit()
